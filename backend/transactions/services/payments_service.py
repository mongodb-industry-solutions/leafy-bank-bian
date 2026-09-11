"""Payments service — the HTTP-facing entry point for the payment lifecycle.

The nine stages live in `process/` and `contexts/`; this class only builds the context and
runs the saga. Read `process/payment_lifecycle.py` for the sequence and the invariants, and
`11-stage-scaffold-plan.md` for why the structure looks like this.

Where the stages are:
    1a capture       contexts/payment_order_initiation/application/capture.py
    2  authenticate  contexts/party_authentication/authenticate.py
    3  validate      contexts/payment_order_initiation/domain/validation.py
    3  enrich        contexts/payment_order_initiation/domain/enrichment.py      [stub]
    4b authorize     contexts/fraud_evaluation/evaluate.py                 [hardcoded]
    1b persist       contexts/payment_order_initiation/application/persist.py
    4a orchestrate   contexts/payment_orchestration/orchestrate.py               [stub]
    5  execute       contexts/payment_rail/execute.py            <- the ACID block
    6  account       (none — ledger service, via change streams)
    7  settle        contexts/payment_settlement/settle.py     [write is inside stage 5]
    8  reconcile     contexts/account_reconciliation/reconcile.py               [stub]
    9  exceptions    process/compensation.py                                    [stub]
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional

from database.connection import MongoDBConnection
from process import payment_lifecycle
from contexts.payment_order_initiation.adapters.mongo_reference_data import (
    MongoReferenceData,
)
from contexts.fraud_evaluation.domain import fraud_rules, sanctions
from contexts.payment_rail.adapters.simulated_wire_rail import SimulatedWireRail
from contexts.payment_order_initiation.domain import checks, lifecycle
from process.payment_context import PaymentCollections, PaymentContext

logger = logging.getLogger(__name__)


class PaymentsService:
    """Runs the payment saga, then reads a payment back with its transaction doc.

    The money move is ONE multi-document ACID transaction on `leafy_bank_bian` — debtor
    balance, creditor balance, one `transactions` doc (payer/payee, v4_21: no legs, no GL),
    the payment status flip, and a sender-side notification. It lives in
    `contexts/payment_rail/execute.py` and must not be restructured.
    """

    def __init__(self, connection: MongoDBConnection, db_name: str, payment_limit_usd: float):
        self.db = connection.get_database(db_name)
        self.customers = self.db["customers"]
        self.accounts = self.db["accounts"]
        self.payments = self.db["payments"]
        self.transactions = self.db["transactions"]
        self.notifications = self.db["notifications"]
        # Stage 4a's collection (doc 18 B1). Not in the canonical spec; named exactly as
        # `payments.refs` declares its FK target. Stage 4b's commitment was folded into
        # `payments.order` per Doina's Aug 27 target model (L427-429) — no collection.
        self.routing_snapshots = self.db["routingSnapshots"]
        # Stage 5's two (doc 19 B2, B3). `paymentMessages` is Doina's rename (L780); the live
        # `canonicalJsonStorage` belongs to fsi-payments-processing and is never touched here.
        self.payment_executions = self.db["paymentExecutions"]
        self.payment_messages = self.db["paymentMessages"]
        self.payment_limit_usd = payment_limit_usd
        # Stage 3's reference-data store (doc 17 §3 step 1). Read-only: seeding is
        # `backend/data/load_reference_seed.py`, run by hand, never by the service.
        self.reference_data = MongoReferenceData(self.db)
        # Stage 5's outbound rail (doc 19 §3 step 3). Simulated by design — *"the demo will
        # not connect to a real payment network"* — and every document it produces says so.
        self.rail_gateway = SimulatedWireRail()

    # --- stage 5 read paths (doc 19 §3 step 7) -------------------------------
    # Projections drop `_id` at the source: echoing a raw ObjectId into a response is the
    # 2026-06-11 defect, and these documents embed a full ISO message, so the read is heavy
    # enough to be worth shaping deliberately.

    def list_payment_executions(self, payment_id: str) -> list:
        """Every execution attempt for a payment, oldest first — append-only, so the order
        is the history."""
        return list(
            self.payment_executions.find({"paymentId": payment_id}, {"_id": 0})
            .sort("attempt", 1)
        )

    def get_payment_execution(self, payment_execution_id: str):
        return self.payment_executions.find_one(
            {"paymentExecutionId": payment_execution_id}, {"_id": 0}
        )

    def get_payment_message(self, payment_message_id):
        if not payment_message_id:
            return None
        return self.payment_messages.find_one(
            {"paymentMessageId": payment_message_id}, {"_id": 0}
        )

    def _collections(self) -> PaymentCollections:
        return PaymentCollections(
            db=self.db,
            customers=self.customers,
            accounts=self.accounts,
            payments=self.payments,
            transactions=self.transactions,
            notifications=self.notifications,
            routing_snapshots=self.routing_snapshots,
            payment_executions=self.payment_executions,
            payment_messages=self.payment_messages,
        )

    def initiate_payment(
        self,
        customer_ref: str,
        debtor_account_ref: str,
        creditor_account_ref: Optional[str],
        instructed_amount: float,
        instructed_currency: str,
        payment_type: str,
        payment_rail: str,
        remittance_unstructured: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        creditor_party: Optional[dict] = None,
        remittance_reference: Optional[str] = None,
        remittance_invoice_no: Optional[str] = None,
        priority: str = "NORMAL",
        charge_bearer: str = "SLEV",
        category_purpose: Optional[str] = None,
        requested_execution_date: Optional[date] = None,
        channel: str = "API",
        authentication: Optional[dict] = None,
        client_reference: Optional[str] = None,
        wire_details: Optional[dict] = None,
        ach_details: Optional[dict] = None,
        internal_details: Optional[dict] = None,
        settlement_outcome: Optional[str] = None,
    ) -> dict:
        """Initiate a payment order. Returns the persisted payment document.

        `creditor_account_ref` is None for an external beneficiary; `creditor_party` then
        carries the snapshot off the request. Everything after the `*` is keyword-only and
        defaulted, so the stage-1 fields are additive for existing callers.

        Raises ValueError on validation failures; caller maps to HTTP 400.
        """
        ctx = PaymentContext(
            customer_ref=customer_ref,
            debtor_account_ref=debtor_account_ref,
            creditor_account_ref=creditor_account_ref,
            creditor_party=creditor_party,
            instructed_amount=instructed_amount,
            instructed_currency=instructed_currency,
            payment_type=payment_type,
            payment_rail=payment_rail,
            remittance_unstructured=remittance_unstructured,
            remittance_reference=remittance_reference,
            remittance_invoice_no=remittance_invoice_no,
            priority=priority,
            charge_bearer=charge_bearer,
            category_purpose=category_purpose,
            requested_execution_date=requested_execution_date,
            channel=channel,
            authentication=authentication,
            client_reference=client_reference,
            idempotency_key=idempotency_key,
            wire_details=wire_details,
            ach_details=ach_details,
            internal_details=internal_details,
            collections=self._collections(),
            payment_limit_usd=self.payment_limit_usd,
            reference_data=self.reference_data,
            rail_gateway=self.rail_gateway,
            settlement_outcome=settlement_outcome,
        )
        return payment_lifecycle.run(ctx)

    def resume_payment(
        self, payment_id: str, *, customer_ref: str, authentication: Optional[dict]
    ) -> dict:
        """Resume a payment HELD at the step-up gate, now with a second factor.

        Re-enters the saga at stage 2 (skips 1 capture — the document already exists), so the
        SAME payment advances through the rest of the lifecycle: one document, one id. This is
        what replaces the old "create a second document on retry" step-up flow (Kiran,
        2026-09-09).
        """
        payment = self.payments.find_one({"paymentId": payment_id})
        if payment is None:
            raise ValueError(f"Payment {payment_id} not found.")
        if not payment.get("stepUpRequired") or payment.get("status") != lifecycle.INITIATED:
            raise ValueError(f"Payment {payment_id} is not awaiting step-up.")
        ctx = self._context_from_doc(
            payment, customer_ref=customer_ref, authentication=authentication
        )
        return payment_lifecycle.run(ctx, start_index=1)

    def _context_from_doc(
        self, payment: dict, *, customer_ref: str, authentication: Optional[dict]
    ) -> PaymentContext:
        """Rebuild the capture-time context from a persisted payment document.

        A held payment has no in-memory context any more, but every later stage reads those
        fields from `ctx` (not from the doc's snapshot). So a resume reconstructs the same
        fields `capture.run` would have set — re-fetching the account/customer documents the
        doc references and re-deriving the flags — so the saga can continue from stage 2.
        """
        colls = self._collections()
        debtor_ref = payment["debtor"]["accountId"]
        creditor = payment.get("creditor") or {}
        creditor_ref = creditor.get("accountId")
        is_external = creditor_ref is None

        debtor_account = colls.accounts.find_one({"accountId": debtor_ref})
        debtor_customer_id = payment["customerId"]
        debtor_customer = colls.customers.find_one({"customerId": debtor_customer_id})
        creditor_account = None
        creditor_customer = None
        creditor_customer_id = None
        if not is_external:
            creditor_account = colls.accounts.find_one({"accountId": creditor_ref})
            if creditor_account:
                creditor_customer_id = (
                    creditor_account.get("customerSnapshot") or {}
                ).get("customerId")
                creditor_customer = colls.customers.find_one(
                    {"customerId": creditor_customer_id}
                )

        remittance = payment.get("remittance") or {}
        initiation = payment.get("initiation") or {}
        rdate = payment.get("requestedExecutionDate")
        requested_execution_date = date.fromisoformat(rdate) if rdate else None

        ctx = PaymentContext(
            customer_ref=customer_ref,
            debtor_account_ref=debtor_ref,
            creditor_account_ref=creditor_ref,
            creditor_party=dict(creditor) if is_external else None,
            instructed_amount=payment["instructedAmount"],
            instructed_currency=payment["instructedCurrency"],
            payment_type=payment["type"],
            payment_rail=payment["rail"],
            remittance_unstructured=remittance.get("unstructured"),
            remittance_reference=remittance.get("reference"),
            remittance_invoice_no=remittance.get("invoiceNo"),
            priority=payment["priority"],
            charge_bearer=payment["chargeBearer"],
            category_purpose=payment.get("categoryPurpose"),
            requested_execution_date=requested_execution_date,
            channel=initiation.get("channel", "API"),
            client_reference=payment.get("clientReference"),
            authentication=authentication,
            wire_details=payment.get("wireDetails"),
            ach_details=payment.get("achDetails"),
            internal_details=payment.get("internalDetails"),
            collections=colls,
            payment_limit_usd=self.payment_limit_usd,
            reference_data=self.reference_data,
            rail_gateway=self.rail_gateway,
        )
        ctx.payment_oid = payment["_id"]
        ctx.payment_id = payment["paymentId"]
        ctx.end_to_end_id = payment["endToEndId"]
        ctx.txn_code = "PMNT-ICDT-BOOK" if payment["rail"] == "INTERNAL" else "PMNT-ICDT-ESCT"
        ctx.is_external_creditor = is_external
        ctx.is_internal = not is_external and debtor_customer_id == creditor_customer_id
        ctx.debtor_account = debtor_account
        ctx.debtor_customer = debtor_customer
        ctx.debtor_customer_id = debtor_customer_id
        ctx.creditor_account = creditor_account
        ctx.creditor_customer = creditor_customer
        ctx.creditor_customer_id = creditor_customer_id
        ctx.payment_doc = payment
        ctx.current_state = payment["status"]
        ctx.now = datetime.now(timezone.utc)
        return ctx

    def retrieve_payment(self, payment_ref: str) -> Optional[dict]:
        """Retrieve a payment plus its single transaction doc (v4_21)."""
        payment = self.payments.find_one({"paymentId": payment_ref})
        if not payment:
            return None
        payment["_txn"] = self.transactions.find_one({"paymentId": payment_ref})
        return payment

    # --- stage 4 read/evaluate operations ------------------------------------

    def evaluate_fraud(self, payment_ref: str) -> Optional[dict]:
        """Re-run the stage-4b rule set over a stored payment. **Persists nothing.**

        The score that authorised the payment is the one written at the AUTHORISED
        transition; this re-evaluation exists so an operator can see which rules fired and
        why. Writing a second score would leave two, with nothing to say which one counted.
        """
        payment = self.payments.find_one({"paymentId": payment_ref})
        if not payment:
            return None

        wire = payment.get("wireDetails") or {}
        creditor = payment.get("creditor") or {}
        purpose_code = (payment.get("remittance") or {}).get("purposeCode")
        debtor_account_id = (payment.get("debtor") or {}).get("accountId")
        external = creditor.get("accountId") is None

        assessment = fraud_rules.assess(
            amount=payment.get("amount"),
            wire_type=wire.get("wireType"),
            purpose_code=purpose_code,
            high_risk_purpose_codes=sanctions.high_risk_purpose_codes(),
            prior_payments_to_beneficiary=self.payments.count_documents(
                fraud_rules.beneficiary_history_filter(
                    debtor_account_id=debtor_account_id,
                    creditor_account_no=creditor.get("accountNo"),
                    exclude_payment_id=payment_ref,
                )
            ),
            debtor_payments_in_window=self.payments.count_documents(
                fraud_rules.velocity_filter(
                    debtor_account_id=debtor_account_id,
                    now=datetime.now(timezone.utc),
                    exclude_payment_id=payment_ref,
                )
            ),
            is_external_creditor=external,
        )
        screening = sanctions.screen(
            creditor_name=creditor.get("name"),
            creditor_country=creditor.get("bankCountry"),
            purpose_code=purpose_code,
        )
        return {
            "paymentId": payment_ref,
            "persisted": False,
            "recordedFraud": payment.get("fraud"),
            "reEvaluated": {
                "score": assessment.score,
                "decision": assessment.decision,
                "rulesFired": assessment.rules_fired,
                "summary": fraud_rules.describe(assessment),
                "rules": [
                    {"name": o.name, "fired": o.fired, "weight": o.weight,
                     "detail": o.detail}
                    for o in assessment.outcomes
                ],
            },
            "sanctions": {"status": screening.status, "detail": screening.detail},
        }

    def confirm_to_originator(self, payment_ref: str) -> Optional[dict]:
        """Re-send the PaymentConfirmation for a payment with a committed execution path.

        Refuses when there is no payment order: confirming an execution path to a customer
        before the bank has committed to one would be a false statement, and her L502 ties
        the confirmation to the commitment specifically.
        """
        payment = self.payments.find_one({"paymentId": payment_ref})
        if not payment:
            return None

        order = payment.get("order") or {}
        order_id = order.get("paymentOrderId")
        if not order_id:
            raise ValueError(
                f"{payment_ref} has no committed execution path to confirm "
                f"(state {(payment.get('lifecycle') or {}).get('currentState')})."
            )

        checks.append_checks(self.payments, payment["_id"], [
            checks.check(
                "4 authorize", "originator_confirmed", checks.PASS,
                mode=checks.ASYNC, actor="payment-confirmation-service",
                detail=(
                    f"Confirmation re-sent for {order_id} "
                    f"({order.get('executionStrategy')}, value date "
                    f"{order.get('valueDate')})."
                ),
            )
        ])
        return {
            "paymentId": payment_ref,
            "paymentOrderId": order_id,
            "executionStrategy": order.get("executionStrategy"),
            "clearingNetwork": order.get("clearingNetwork"),
            "valueDate": order.get("valueDate"),
            "confirmed": True,
        }

    # --- stage 7 settlement operation -----------------------------------------

    def settle_payment(self, payment_ref: str, outcome: Optional[str] = None) -> Optional[dict]:
        """Trigger or re-trigger settlement for one payment (doc 21 step 7, B6).

        Loads the payment, reconstructs enough context for `settle.run`, and calls it.
        Returns the updated payment doc, or None when the payment is not found.

        Refuses when the payment is not at IN_PROGRESS — settling an already-settled
        or rejected payment would be a false state transition.
        """
        from contexts.payment_orchestration.domain.routing import ExecutionStrategy
        from contexts.payment_settlement import settle
        from process.payment_context import PaymentContext, PaymentCollections

        payment = self.payments.find_one({"paymentId": payment_ref})
        if not payment:
            return None

        state = (payment.get("lifecycle") or {}).get("currentState")
        if state != "IN_PROGRESS":
            raise ValueError(
                f"{payment_ref} is at {state}, not IN_PROGRESS — settlement can only be "
                f"triggered for a payment awaiting settlement."
            )

        # Reconstruct the routing strategy from the routing snapshot, for model selection.
        routing_id = (payment.get("refs") or {}).get("routingSnapshotId")
        correspondent = {}
        if routing_id:
            snap = self.routing_snapshots.find_one({"routingSnapshotId": routing_id}) or {}
            correspondent = snap.get("correspondent") or {}

        strategy = ExecutionStrategy(
            strategy="RECONSTRUCTED", network=None,
            cost_rank="UNKNOWN",
            requires_correspondent=bool(correspondent.get("required")),
            correspondent_bic=correspondent.get("bic"),
            cutoff_hour_et=None, within_cutoff=True,
            value_date=None, rationale="reconstructed for settlement",
        )

        ctx = PaymentContext(
            customer_ref=(payment.get("debtor") or {}).get("customerId", ""),
            debtor_account_ref=(payment.get("debtor") or {}).get("accountId", ""),
            creditor_account_ref=(payment.get("creditor") or {}).get("accountId"),
            creditor_party=None,
            instructed_amount=payment.get("amount", 0),
            instructed_currency=payment.get("currency", "USD"),
            payment_type=payment.get("paymentType", "CREDIT_TRANSFER"),
            payment_rail=payment.get("rail", "WIRE"),
            collections=self._collections(),
            payment_limit_usd=self.payment_limit_usd,
            execution_strategy=strategy,
            settlement_outcome=outcome,
        )
        ctx.payment_oid = payment["_id"]
        ctx.payment_id = payment_ref
        ctx.payment_doc = payment
        ctx.current_state = state
        ctx.is_external_creditor = (payment.get("creditor") or {}).get("accountId") is None

        settle.run(ctx)
        return ctx.payment_doc
