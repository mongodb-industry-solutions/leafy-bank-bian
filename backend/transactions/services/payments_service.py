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

import dataclasses
import logging
import random
from datetime import date, datetime, timezone
from typing import Optional

from pymongo.errors import DuplicateKeyError

from database.connection import MongoDBConnection
from process import inbound_lifecycle, payment_lifecycle
from process.exceptions import (
    ACTION_ACCEPT_DISCREPANCY,
    ACTION_DISMISS,
    ACTION_ESCALATE_TO_CORRESPONDENT,
    ACTION_LINK_STATEMENT_ENTRY,
    ACTION_POST_ADJUSTMENT,
    ACTION_RECHECK,
    ACTION_RETRY_SETTLEMENT,
    ACTION_RETURN_FUNDS,
    CATEGORY_DUPLICATE_SIGNAL,
    CATEGORY_ORPHANED_SETTLEMENT,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_RECONCILIATION_MISSING,
    CATEGORY_SETTLEMENT_DELAYED,
    CATEGORY_SETTLEMENT_RETURNED,
    CATEGORY_SETTLEMENT_UNMATCHED,
    CATEGORY_UTA,
    ExceptionActionNotLegal,
    ExceptionConflict,
    ExceptionNotFound,
    STATUS_DISMISSED,
    STATUS_OPEN,
    STATUS_RESOLVED,
)
from contexts.payment_order_initiation.adapters.mongo_reference_data import (
    MongoReferenceData,
)
from contexts.fraud_evaluation.domain import fraud_rules, sanctions
from contexts.payment_rail.adapters.simulated_wire_rail import SimulatedWireRail
from contexts.payment_order_initiation.domain import checks, lifecycle
from process.payment_context import PaymentCollections, PaymentContext
from contexts.payment_orchestration.domain import cutoff_policy
from shared import business_clock

logger = logging.getLogger(__name__)


def _settlement_adjustment_for(payment: dict, exc: dict) -> Optional[dict]:
    """The correcting entry an accepted short-settlement needs, keyed off `chargeBearer`.

    Sep 17 L1264-1270: the rail settled short while the GL posted the full amount. Who
    bears the correspondent's charge decides whether the bank books anything:
      * DEBT — Leafy Bank's customer pays all charges, so the bank absorbs the deduction:
        post it (Dr 5214 Correspondent Charges / Cr nostro).
      * CRED / SHAR / SLEV — the beneficiary bears (or shares under scheme rules) the
        correspondent's charge; the short credit is theirs, no bank entry. The accept is
        the record.
    Returns None when no entry is due.
    """
    if payment.get("chargeBearer") != "DEBT":
        return None
    amount = ((exc.get("detail") or {}).get("discrepancyAmount")) or 0
    if amount <= 0:
        return None
    return {
        "amount": amount,
        "currency": payment.get("currency", "USD"),
        "chargeBearer": "DEBT",
        "exceptionId": exc.get("exceptionId"),
    }


class PaymentNotFound(LookupError):
    """No payment with that id. The cutoff-hold routes map this to 404."""


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
        statement_outcome: Optional[str] = None,
        clock_run_id: Optional[str] = None,
    ) -> dict:
        """Initiate a payment order. Returns the persisted payment document.

        `creditor_account_ref` is None for an external beneficiary; `creditor_party` then
        carries the snapshot off the request. Everything after the `*` is keyword-only and
        defaulted, so the stage-1 fields are additive for existing callers.

        `clock_run_id` tags the payment to a demo clock run (`shared.business_clock`). It is
        deliberately NOT on the API request model: only in-process scenario code may tag.

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
            # Reconciliation plan Decision 1: UNMATCHED is an alias for a fee-deducted
            # correspondent statement line — the statement is the only source of the $25.
            statement_outcome=("FEE_DEDUCTED" if settlement_outcome == "UNMATCHED"
                               else statement_outcome),
            clock_run_id=clock_run_id,
        )
        return payment_lifecycle.run(ctx)

    def generate_statement(self, *, account_code: str = "1111", include_orphan: bool = True,
                           skip_presenter_driven: bool = False) -> Optional[dict]:
        """The correspondent's next camt.053 for a nostro (reconciliation plan A1).

        One code path for the background worker and the manual route, like
        `simulate_inbound`. Returns None when no settled wire is waiting to be booked.
        """
        from contexts.financial_gateway.application.statement import generate_statement
        return generate_statement(self.db, account_code=account_code,
                                  include_orphan=include_orphan,
                                  skip_presenter_driven=skip_presenter_driven)

    def simulate_inbound(self, scenario: str = "HAPPY", *, account_id: Optional[str] = None) -> dict:
        """Generate one simulated inbound pacs.008 and run it (demo trigger).

        One code path for both triggers — the background worker and the manual route — so
        the two can never drift apart. Picks a live customer account as the claimed
        beneficiary, builds the message via `simulate.build_message` (which names the
        scenario's mutation), and hands it to `receive_inbound`.

        `account_id` pins the beneficiary, which exists for the tests: `random.choice` over
        live accounts is right for a demo trigger and wrong for an assertion, which would
        be grading a scenario against whichever account the draw happened to pick.

        `DUPLICATE` runs the SAME message twice and returns the FIRST result: the second is
        an idempotent replay that returns the same payment, and that identity IS the demo
        beat ("the same wire arriving twice credits the customer once").
        """
        from contexts.financial_gateway.domain import simulate

        if scenario.upper() not in simulate.SCENARIOS:
            raise ValueError(
                f"Unknown inbound scenario {scenario!r} "
                f"(legal: {list(simulate.SCENARIOS)})."
            )

        # ⚠️ Customer accounts only, and by type — the shared `accounts` collection also
        # holds NOSTRO/VOSTRO/GL_ACCOUNT bank-internal accounts, and a simulated wire
        # naming the clearing account as its beneficiary is not a demo beat, it is a
        # corruption of the mirror posting (defect 2026-06-29's rule, read-side edition).
        # Projected: on a shared database, an unprojected list read is the 2026-08-31
        # 37 MB failure, and this loop needs four fields.
        candidates = list(self.accounts.find(
            {
                "type": {"$in": ["CURRENT", "SAVINGS", "CHECKING"]},
                "status": "ACTIVE",
            },
            {"accountId": 1, "accountNumber": 1, "iban": 1, "currency": 1,
             "customerSnapshot.customerId": 1, "type": 1, "status": 1},
        ))
        if account_id is not None:
            candidates = [a for a in candidates if a.get("accountId") == account_id]
        if not candidates:
            raise ValueError(
                "No active customer account to name as the inbound beneficiary — "
                "seed accounts first."
            )
        account = random.choice(candidates)

        customer = self.customers.find_one(
            {"customerId": (account.get("customerSnapshot") or {}).get("customerId")}
        )
        holder = ((customer or {}).get("identification") or {}).get("legalName")
        if not holder:
            raise ValueError(
                f"Account {account.get('accountId')} has no named holder on record — "
                "the beneficiary name match has nothing to match against."
            )

        identifier = account.get("iban") or account.get("accountNumber")
        message = simulate.build_message(
            scenario=scenario.upper(),
            beneficiary_name=holder,
            beneficiary_identifier=identifier,
            identifier_is_iban=bool(account.get("iban")),
            account_currency=account.get("currency") or "USD",
        )

        first = self.receive_inbound(message)
        if scenario.upper() != simulate.SCENARIO_DUPLICATE:
            return first
        replay = self.receive_inbound(message)
        if replay.get("paymentId") != first.get("paymentId"):
            # Not an assertion about idempotency's correctness — the hermetic suite owns
            # that. This is the trigger reporting honestly what it produced, because the
            # whole point of the DUPLICATE scenario is that both sends are one payment.
            logger.warning(
                "simulate_inbound: duplicate scenario produced two payment ids (%s, %s)",
                first.get("paymentId"), replay.get("paymentId"),
            )
        return first

    def receive_inbound(self, message: dict) -> dict:
        """Receive an external pacs.008 and run the inbound lifecycle (FR-1.IN1..3).

        The inbound counterpart of `initiate_payment`, and the entry point behind
        `POST /FinancialGateway/{id}/Inbound/Initiate`. Takes a MESSAGE, not a payment
        request: there is no customer, no account selection and no rail choice on this path
        (her L385 — the rail is fixed by the channel the message arrived on).

        Raises `ValueError` (incl. `MessageRejected`) on an unusable message; the route maps
        it to 400. A rejected message is still persisted first — see `receive.run`.
        """
        ctx = PaymentContext(
            # No customer and no debtor account: the originator banks elsewhere. These are
            # required positional fields on the context, so they are explicitly empty rather
            # than absent — `receive.run` fills what the message actually yields.
            customer_ref="",
            debtor_account_ref="",
            instructed_amount=0.0,
            instructed_currency="",
            payment_type="",
            payment_rail="",
            direction="INBOUND",
            collections=self._collections(),
            payment_limit_usd=self.payment_limit_usd,
            reference_data=self.reference_data,
            rail_gateway=self.rail_gateway,
        )
        ctx.inbound_message = message
        return inbound_lifecycle.run(ctx)

    def resume_inbound(self, payment_id: str) -> dict:
        """Resume an inbound payment after a UTA Repair (FR-9.IN2).

        Re-enters at stage 3 (her L1336: *"the payment resumes at Stage 3"*), because the
        operator has just supplied what stage 2 could not resolve.

        ⚠️ The correction is read back **from the document**, never from a caller argument:
        `uta.repair` persists `beneficiaryResolution` before calling this, and
        `_inbound_context_from_doc` rebuilds the context from what is stored. That is the
        whole point of the split — defect 2026-09-28 (`control-not-persisted-across-reentry`)
        is what happens when a resume trusts memory instead.
        """
        payment = self.payments.find_one({"paymentId": payment_id})
        if payment is None:
            raise ValueError(f"Payment {payment_id} not found.")
        if payment.get("direction") != "INBOUND":
            raise ValueError(f"Payment {payment_id} is not an inbound payment.")
        ctx = self._inbound_context_from_doc(payment)
        return inbound_lifecycle.run(
            ctx, start_index=inbound_lifecycle.REPAIR_RESUME_INDEX
        )

    def _inbound_context_from_doc(self, payment: dict) -> PaymentContext:
        """Rebuild an inbound context from a persisted payment.

        The inbound twin of `_context_from_doc`, and it restores the same class of thing:
        every field a later stage reads off `ctx` rather than off the document. The two that
        matter most here are `beneficiary_match` and the resolved creditor account — both
        written by stage 2 (or by an operator's Repair) and both lost if this trusted the
        in-memory context instead of the stored one.
        """
        colls = self._collections()
        resolution = payment.get("beneficiaryResolution") or {}
        creditor_ref = resolution.get("matchedAccountId") or (
            payment.get("creditor") or {}
        ).get("accountId")

        creditor_account = (
            colls.accounts.find_one({"accountId": creditor_ref}) if creditor_ref else None
        )
        creditor_customer_id = (
            (creditor_account or {}).get("customerSnapshot") or {}
        ).get("customerId")
        creditor_customer = (
            colls.customers.find_one({"customerId": creditor_customer_id})
            if creditor_customer_id else None
        )

        remittance = payment.get("remittance") or {}
        debtor = payment.get("debtor") or {}
        ctx = PaymentContext(
            customer_ref=creditor_customer_id or "",
            debtor_account_ref="",
            instructed_amount=payment.get("amount") or payment["instructedAmount"],
            instructed_currency=payment.get("currency") or payment["instructedCurrency"],
            payment_type=payment["type"],
            payment_rail=payment["rail"],
            direction="INBOUND",
            remittance_unstructured=remittance.get("unstructured"),
            remittance_reference=remittance.get("reference"),
            charge_bearer=payment["chargeBearer"],
            collections=colls,
            payment_limit_usd=self.payment_limit_usd,
            reference_data=self.reference_data,
            rail_gateway=self.rail_gateway,
        )
        ctx.payment_oid = payment["_id"]
        ctx.payment_id = payment["paymentId"]
        ctx.end_to_end_id = payment["endToEndId"]
        ctx.txn_code = "PMNT-RCDT-ESCT"
        ctx.payment_doc = payment
        ctx.current_state = payment["status"]
        ctx.now = datetime.now(timezone.utc)
        ctx.creditor_account = creditor_account
        ctx.creditor_account_ref = creditor_ref
        ctx.creditor_customer = creditor_customer
        ctx.creditor_customer_id = creditor_customer_id
        # The stage-2 outcome, restored from the document — including a Repair's MATCHED.
        ctx.beneficiary_match = resolution.get("matchOutcome")
        ctx.claimed_creditor = dict(payment.get("creditor") or {})
        # Enough of the parse for stages 3-7 to re-read the originator. The stored debtor
        # snapshot IS the parsed originator (stage 1 built one from the other), so this
        # reconstructs the fields those stages actually touch rather than re-parsing the
        # raw message.
        ctx.inbound_parsed = {
            "debtor": debtor,
            "purposeCode": remittance.get("purposeCode"),
            "settlementDate": (payment.get("clearing") or {}).get("settlementDate"),
            **(payment.get("senderReferences") or {}),
        }
        ctx.inbound_message_id = (payment.get("refs") or {}).get("canonicalJsonId")
        return ctx

    def resolve_uta(
        self,
        exception_id: str,
        *,
        action: str,
        matched_account_id: Optional[str] = None,
        return_reason_code: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict:
        """Resolve one Unable-to-Apply exception (FR-9.IN2). Returns the updated exception.

        Guard chain, mirroring `resolve_exception`: exception exists -> OPEN -> category is
        UTA -> payment exists -> run the action -> mark resolved.

        ## Ordering: the claim is written LAST here, not first

        `resolve_exception`'s RETRY path resolves BEFORE re-driving, because its producer
        dedupes on the open row (defect 2026-09-23 B5). Neither UTA action has that problem
        — a Repair resumes the saga and a Return closes the payment, and neither re-runs the
        producer that opened this exception. So the safer ordering applies: do the work, and
        only mark the exception resolved once it has actually succeeded. A failure leaves
        the row OPEN and retryable, which is defect 2026-09-28 A4's requirement met by
        construction rather than by a rollback.
        """
        from contexts.financial_gateway.application import uta

        exc = self.db["exceptions"].find_one({"exceptionId": exception_id})
        if exc is None:
            raise ValueError(f"Exception {exception_id} not found.")
        if exc["status"] != STATUS_OPEN:
            raise ValueError(
                f"Exception {exception_id} is {exc['status']}, not OPEN — only an open "
                "exception can be resolved."
            )
        if exc["category"] != CATEGORY_UTA:
            raise ValueError(
                f"Action {action} is not legal for a {exc['category']} exception — "
                "REPAIR and RETURN apply only to an Unable-to-Apply exception."
            )

        payment = self.payments.find_one({"paymentId": exc["paymentId"]})
        if payment is None:
            raise ValueError(
                f"Payment {exc['paymentId']} for exception {exception_id} not found."
            )

        if action == "REPAIR":
            uta.repair(
                self, exc, payment,
                matched_account_id=matched_account_id, note=note,
            )
        else:  # RETURN
            uta.build_return(
                self, payment, return_reason_code=return_reason_code,
            )

        self._mark_exception_resolved(
            exc, action, note, datetime.now(timezone.utc),
        )
        return self.db["exceptions"].find_one({"exceptionId": exception_id})

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
        # The hold is over once the saga re-enters; a later stage-2 hold sets them again.
        self.payments.update_one(
            {"_id": payment["_id"]}, {"$unset": {"stepUpRequired": "", "stepUpReason": ""}}
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
            # B4: restore the stage-7 simulation lever so a resumed wire does not reset
            # UNMATCHED/DELAYED/EXCEPTION to MATCHED. Persisted at initiation; read by
            # settle.run. None for non-wire or pre-B4 docs (→ MATCHED, the safe default).
            settlement_outcome=payment.get("simulatedSettlementOutcome"),
            # Same B4 rule for the statement lever (plan A1).
            statement_outcome=payment.get("simulatedStatementOutcome"),
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
        # Restore the demo clock tag before reading the clock, so a resumed payment stays on
        # its run's business time.
        ctx.clock_run_id = (payment.get("demo") or {}).get("clockRunId")
        ctx.now = business_clock.ctx_now(ctx)
        # Cutoff plan A2 (B4 rule): a decision taken on a hold must survive the next resume.
        screening = payment.get("screening") or {}
        cutoff = payment.get("cutoff") or {}
        ctx.screening_override = screening.get("outcome")
        ctx.cutoff_decision = cutoff.get("decision")
        if cutoff.get("decision") == "EXPEDITE":
            ctx.value_date_override = cutoff.get("valueDate")
        return ctx

    def _reattach_routing(self, ctx: PaymentContext, payment: dict) -> None:
        """Re-attach stage 4a's outputs from the persisted routing snapshot.

        A resume rebuilds capture-time fields, not the strategy orchestrate decided, so the
        order commit in stage 4b has nothing to read otherwise. The snapshot itself stays
        immutable: an expedited value date is applied to the in-memory strategy only.
        """
        from contexts.payment_orchestration.domain.routing import ExecutionStrategy
        routing_id = (payment.get("refs") or {}).get("routingSnapshotId")
        snap = (
            self.routing_snapshots.find_one({"routingSnapshotId": routing_id})
            if routing_id else None
        )
        if snap:
            corr = snap.get("correspondent") or {}
            strategy = ExecutionStrategy(
                strategy=snap.get("executionStrategy"),
                network=snap.get("clearingNetwork"),
                cost_rank=snap.get("costRank"),
                requires_correspondent=bool(corr.get("required")),
                correspondent_bic=corr.get("bic"),
                cutoff_hour_et=snap.get("cutoffHourET"),
                within_cutoff=bool(snap.get("withinCutoff", True)),
                value_date=snap.get("valueDate"),
                rationale=snap.get("rationale"),
            )
            if ctx.value_date_override:
                strategy = dataclasses.replace(
                    strategy, value_date=ctx.value_date_override, within_cutoff=True,
                )
            ctx.execution_strategy = strategy
        ctx.routing_snapshot_id = routing_id

    def resolve_review(
        self, payment_id: str, *, decision: str, actor: str = "operator-review"
    ) -> dict:
        """Resolve a payment HELD at MANUAL_FRAUD_REVIEW by an operator's manual-review decision.

        FR-4.13. `decision` is "APPROVED" or "REJECTED".

        APPROVED re-enters the saga at stage 4b (authorize) with a review override: the
        model's REVIEW assessment stays on the document, the operator's approval commits the
        authorisation the model withheld, and the payment continues through stage 5 (execute)
        to settlement. Same document, same id.

        REJECTED terminates the payment to REJECTED — a pre-execution terminal, no money has
        moved — with an operator-review check and reason.
        """
        payment = self.payments.find_one({"paymentId": payment_id})
        if payment is None:
            raise ValueError(f"Payment {payment_id} not found.")
        if payment.get("status") != lifecycle.MANUAL_FRAUD_REVIEW:
            raise ValueError(
                f"Payment {payment_id} is at {payment.get('status')}, not MANUAL_FRAUD_REVIEW — "
                "only a payment held for manual review can be resolved."
            )

        if decision == "REJECTED":
            checks.append_checks(self.payments, payment["_id"], [
                checks.check(
                    "4 authorize", "manual_review_declined", checks.FAIL,
                    detail="Operator declined a payment held for manual review.",
                    actor=actor, at=datetime.now(timezone.utc),
                )
            ])
            updated = lifecycle.reject(
                self.payments, payment["_id"],
                reason="Declined by operator manual review.", actor=actor,
            )
            return updated or self.payments.find_one({"paymentId": payment_id})

        if decision != "APPROVED":
            raise ValueError(
                f"Unknown review decision {decision!r} (expected APPROVED or REJECTED)."
            )

        # Rebuild the capture-time context, then re-attach stage 4a's outputs from the
        # persisted routing snapshot — a resume rebuilds capture-time fields, not the
        # strategy that orchestrate decided, so the order commit has nothing to read
        # otherwise. Same reconstruction shape as `settle_payment`.
        ctx = self._context_from_doc(
            payment, customer_ref=payment.get("customerId", ""), authentication=None,
        )
        self._reattach_routing(ctx, payment)
        ctx.review_override = "APPROVED"
        return payment_lifecycle.run(ctx, start_index=5)

    # --- cutoff plan A2: the four holds' decisions ----------------------------
    #
    # Every method here refuses an untagged payment (ValueError → 400) and raises
    # PaymentNotFound (→ 404) for a missing one. The holds are reachable only by a tagged
    # payment, so an untagged one is never in a held state; the explicit refusal keeps these
    # routes from ever acting on real traffic.

    def _held_payment(self, payment_id: str, allowed: tuple) -> dict:
        payment = self.payments.find_one({"paymentId": payment_id})
        if payment is None:
            raise PaymentNotFound(f"Payment {payment_id} not found.")
        if not (payment.get("demo") or {}).get("clockRunId"):
            raise ValueError(
                f"Payment {payment_id} is not a demo-clock payment; holds apply only to "
                "tagged payments."
            )
        if payment.get("status") not in allowed:
            raise ValueError(
                f"Payment {payment_id} is at {payment.get('status')}, not "
                f"{' or '.join(allowed)}."
            )
        return payment

    def _held_context(self, payment: dict) -> PaymentContext:
        return self._context_from_doc(
            payment, customer_ref=payment.get("customerId", ""), authentication=None,
        )

    def approve_payment(self, payment_id: str, *, approver_id: str, decision: str) -> dict:
        """A second signatory's decision on a PENDING_APPROVAL hold (D1).

        APPROVED records the approver and re-enters the saga at stage 3 (validate), so funds
        and the cut-off are checked at release time, not at initiation. REJECTED terminates.
        """
        payment = self._held_payment(payment_id, (lifecycle.PENDING_APPROVAL,))
        if approver_id == payment.get("customerId"):
            raise ValueError("The initiator cannot approve their own payment.")
        account = self.accounts.find_one({"accountId": payment["debtor"]["accountId"]}) or {}
        signatories = {s.get("customerId") for s in account.get("signatories") or []}
        if approver_id not in signatories:
            raise ValueError(
                f"{approver_id} is not a signatory on account {account.get('accountId')}."
            )

        ctx = self._held_context(payment)
        now = business_clock.ctx_now(ctx)
        if decision == "REJECTED":
            checks.append_checks(self.payments, payment["_id"], [checks.check(
                "2 authenticate", "dual_approval", checks.FAIL,
                detail=f"Second signatory {approver_id} rejected the payment.",
                actor=approver_id, at=now,
            )])
            updated = lifecycle.reject(
                self.payments, payment["_id"],
                reason=f"Rejected by second signatory {approver_id}.", actor=approver_id,
            )
            return updated or self.payments.find_one({"paymentId": payment_id})
        if decision != "APPROVED":
            raise ValueError(f"Unknown approval decision {decision!r}.")

        approval = {
            "entitlement.dualApprovalBy": approver_id,
            "entitlement.dualApproval.status": checks.PASS,
            "entitlement.dualApproval.approvers": [approver_id],
            "entitlement.dualApproval.approvedBy": approver_id,
            "entitlement.dualApproval.approvedAt": now,
        }
        self.payments.update_one({"_id": payment["_id"]}, {"$set": approval})
        checks.append_checks(self.payments, payment["_id"], [checks.check(
            "2 authenticate", "dual_approval", checks.PASS,
            detail=f"Approved by second signatory {approver_id}.",
            actor=approver_id, at=now,
        )])
        ctx.payment_doc = self.payments.find_one({"_id": payment["_id"]})
        return payment_lifecycle.run(ctx, start_index=2)

    def recheck_funds(self, payment_id: str) -> tuple:
        """Re-test a PENDING_FUNDS hold (D2). Returns `(payment, still_short_by)`.

        Still short: no transition, `still_short_by` > 0. Covered: re-enter at stage 3, which
        re-runs the whole validation (including the funds check) against the fresh balance.
        """
        payment = self._held_payment(payment_id, (lifecycle.PENDING_FUNDS,))
        ctx = self._held_context(payment)
        available = ((ctx.debtor_account or {}).get("balance") or {}).get("available", 0)
        short_by = ctx.instructed_amount - available
        if short_by > 0:
            return payment, short_by
        return payment_lifecycle.run(ctx, start_index=2), None

    def resolve_screening(self, payment_id: str, *, analyst_id: str, outcome: str) -> dict:
        """An analyst's decision on a PENDING_SCREENING hold (D4).

        HIT rejects. CLEAR is persisted, then: a recorded next-value-date hold warehouses at
        ROUTED; past the internal cut-off diverts to CUTOFF_EXCEPTION (`resumeFrom: "4b"`);
        otherwise the saga re-enters at stage 4b, which skips the screen and still scores.
        """
        payment = self._held_payment(payment_id, (lifecycle.PENDING_SCREENING,))
        ctx = self._held_context(payment)
        now = business_clock.ctx_now(ctx)
        resolution = {
            "screening.status": outcome,
            "screening.outcome": outcome,
            "screening.resolvedBy": analyst_id,
            "screening.resolvedAt": now,
            "correspondent.sanctionsCheck.status": outcome,
            "correspondent.sanctionsCheck.checkedAt": now,
        }
        if outcome == sanctions.HIT:
            self.payments.update_one({"_id": payment["_id"]}, {"$set": resolution})
            checks.append_checks(self.payments, payment["_id"], [checks.check(
                "4 authorize", "sanctions_screening", checks.FAIL,
                detail=f"Analyst {analyst_id} confirmed the potential match as a HIT.",
                actor=analyst_id, at=now,
            )])
            updated = lifecycle.reject(
                self.payments, payment["_id"],
                reason=f"Sanctions HIT confirmed by analyst {analyst_id}.", actor=analyst_id,
            )
            return updated or self.payments.find_one({"paymentId": payment_id})
        if outcome != sanctions.CLEAR:
            raise ValueError(f"Unknown screening outcome {outcome!r}.")

        if ctx.cutoff_decision == "HOLD_NEXT_VALUE_DATE":
            return lifecycle.advance(
                self.payments, payment["_id"], lifecycle.ROUTED,
                actor=analyst_id, from_state=lifecycle.PENDING_SCREENING, at=now,
                reason="Screening cleared; warehoused for the next value date already chosen.",
                extra=resolution,
            )

        window = self._cutoff_window(payment)
        if window is not None and cutoff_policy.phase(window, at=now) != cutoff_policy.BEFORE_INTERNAL:
            today = business_clock.to_et(now).date()
            return lifecycle.advance(
                self.payments, payment["_id"], lifecycle.CUTOFF_EXCEPTION,
                actor=analyst_id, from_state=lifecycle.PENDING_SCREENING, at=now,
                reason="Screening cleared after the internal cut-off; awaiting a cut-off "
                       "decision.",
                extra={
                    **resolution,
                    "cutoff.wireType": window.wire_type,
                    "cutoff.internalCutoffAt": cutoff_policy.cutoff_at(
                        window, business_date=today, which="internal"),
                    "cutoff.externalCutoffAt": cutoff_policy.cutoff_at(
                        window, business_date=today, which="external"),
                    "cutoff.phaseAtCheck": cutoff_policy.phase(window, at=now),
                    "cutoff.trippedAt": now,
                    "cutoff.resumeFrom": "4b",
                },
            )

        self.payments.update_one({"_id": payment["_id"]}, {"$set": resolution})
        ctx.payment_doc = self.payments.find_one({"_id": payment["_id"]})
        ctx.screening_override = sanctions.CLEAR
        self._reattach_routing(ctx, payment)
        return payment_lifecycle.run(ctx, start_index=5)

    def decide_cutoff(self, payment_id: str, *, decision: str, decided_by: str) -> dict:
        """A decision on a CUTOFF_EXCEPTION hold (D3).

        EXPEDITE: today's value date, back to ROUTED, re-enter at stage 4b (index 5 — the
        routing snapshot already exists and is insert-only). Refused past the external
        cut-off, or while the payment has an OPEN exception. NEXT_VALUE_DATE: the next
        business day, back to ROUTED, warehoused there (no release worker).
        """
        payment = self._held_payment(payment_id, (lifecycle.CUTOFF_EXCEPTION,))
        ctx = self._held_context(payment)
        now = business_clock.ctx_now(ctx)
        today = business_clock.to_et(now).date()
        window = self._cutoff_window(payment)

        if decision == "EXPEDITE":
            if window is not None and cutoff_policy.phase(window, at=now) == cutoff_policy.AFTER_EXTERNAL:
                raise ValueError("Past the external cut-off — the payment cannot be expedited.")
            if self.db["exceptions"].find_one({"paymentId": payment_id, "status": STATUS_OPEN}):
                raise ValueError(f"Payment {payment_id} has an OPEN exception.")
            value_date = today.isoformat()
            reason = f"Expedited by {decided_by}; value date {value_date}."
        elif decision == "NEXT_VALUE_DATE":
            value_date = cutoff_policy.next_business_day(today).isoformat()
            reason = f"Next value date {value_date} chosen by {decided_by}; warehoused."
        else:
            raise ValueError(f"Unknown cut-off decision {decision!r}.")

        extra = {
            "cutoff.decision": decision,
            "cutoff.decidedBy": decided_by,
            "cutoff.decidedAt": now,
            "cutoff.valueDate": value_date,
        }
        if decision == "NEXT_VALUE_DATE":
            extra["requestedExecutionDate"] = value_date
        updated = lifecycle.advance(
            self.payments, payment["_id"], lifecycle.ROUTED,
            actor=decided_by, reason=reason, from_state=lifecycle.CUTOFF_EXCEPTION,
            at=now, extra=extra,
        )
        if decision == "NEXT_VALUE_DATE":
            return updated

        ctx = self._held_context(updated)
        self._reattach_routing(ctx, updated)
        return payment_lifecycle.run(ctx, start_index=5)

    def hold_next_value_date(self, payment_id: str, *, decided_by: str, reason: str) -> dict:
        """Record a next-value-date decision on a pending hold. No transition.

        The payment stays in its hold; when released it is warehoused at ROUTED for the
        next business day (`requestedExecutionDate` makes orchestrate warehouse it, and a
        screening release checks `cutoff.decision`).
        """
        payment = self._held_payment(payment_id, (
            lifecycle.PENDING_APPROVAL, lifecycle.PENDING_FUNDS, lifecycle.PENDING_SCREENING,
        ))
        now = business_clock.ctx_now(self._held_context(payment))
        value_date = cutoff_policy.next_business_day(business_clock.to_et(now).date()).isoformat()
        self.payments.update_one({"_id": payment["_id"]}, {"$set": {
            "cutoff.decision": "HOLD_NEXT_VALUE_DATE",
            "cutoff.decidedBy": decided_by,
            "cutoff.decidedAt": now,
            "cutoff.valueDate": value_date,
            "cutoff.reason": reason,
            "requestedExecutionDate": value_date,
            "updatedAt": now,
        }})
        return self.payments.find_one({"_id": payment["_id"]})

    @staticmethod
    def _cutoff_window(payment: dict):
        return cutoff_policy.window_for(
            rail=payment.get("rail"),
            wire_type=(payment.get("wireDetails") or {}).get("wireType"),
            currency=payment.get("currency") or payment.get("instructedCurrency"),
        )

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

        confirmation = payment.get("confirmation") or {}
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
            "confirmationId": confirmation.get("confirmationId"),
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

        # defer=False: an operator-triggered settlement (or retry of a DELAYED wire) settles
        # immediately — the deferred window is for the saga's automatic default-wire path only.
        settle.run(ctx, defer=False)
        return ctx.payment_doc

    # --- Stage 9: exceptions resolve (doc 24 B4/B6) --------------------------

    def resolve_exception(
        self,
        exception_id: str,
        *,
        action: str,
        note: Optional[str] = None,
        new_settlement_outcome: Optional[str] = None,
    ) -> dict:
        """Resolve one queued exception (doc 24 B4). Resolution is evidence alongside the
        payment's terminal state, never a state change — FAILED stays FAILED even after
        RETURN_FUNDS restores the debtor (B4). The one exception is RETRY_SETTLEMENT on a
        DELAYED payment (still IN_PROGRESS, not terminal): re-driving settlement is a
        legitimate forward transition, not a reopened terminal.

        Guard chain (mirrors `resolve_review`): exception exists → status OPEN (409
        otherwise) → action legal for the category (422 otherwise) → run the action → write
        `resolution{}` + status RESOLVED|DISMISSED → append a `checks[]` entry on the payment.

        Returns the updated exception doc. Raises `ValueError` for not-found / not-OPEN /
        action-not-legal; the router maps those to 404 / 409 / 422.
        """
        from process.compensation import return_of_funds

        # B4's table — which action is legal for which category. RETRY on a FAILED payment
        # is deliberately NOT offered: FAILED is final; the correction is accept or return.
        _LEGAL = {
            CATEGORY_SETTLEMENT_DELAYED: {ACTION_RETRY_SETTLEMENT},
            CATEGORY_SETTLEMENT_UNMATCHED: {ACTION_RETURN_FUNDS, ACTION_ACCEPT_DISCREPANCY},
            CATEGORY_SETTLEMENT_RETURNED: {ACTION_RETURN_FUNDS},
            # Plan A4. The bearer gate below narrows DISCREPANCY to one closing action.
            CATEGORY_RECONCILIATION_DISCREPANCY: {
                ACTION_ACCEPT_DISCREPANCY, ACTION_POST_ADJUSTMENT, ACTION_ESCALATE_TO_CORRESPONDENT,
            },
            CATEGORY_RECONCILIATION_MISSING: {ACTION_ESCALATE_TO_CORRESPONDENT},
            CATEGORY_ORPHANED_SETTLEMENT: {ACTION_ESCALATE_TO_CORRESPONDENT, ACTION_DISMISS},
            CATEGORY_DUPLICATE_SIGNAL: {ACTION_DISMISS},
        }

        exc = self.db["exceptions"].find_one({"exceptionId": exception_id})
        if exc is None:
            raise ExceptionNotFound(f"Exception {exception_id} not found.")
        if exc["status"] != STATUS_OPEN:
            raise ExceptionConflict(
                f"Exception {exception_id} is {exc['status']}, not OPEN — only an open "
                "exception can be resolved."
            )
        category = exc["category"]
        if action in (ACTION_RECHECK, ACTION_LINK_STATEMENT_ENTRY):
            # A4 D1a — both run where the statement, position and tie-out live.
            raise ExceptionActionNotLegal(
                f"{action} runs on the ledger: POST /pipeline/exceptions/{exception_id}/"
                f"{'recheck' if action == ACTION_RECHECK else 'link'}."
            )
        legal = _LEGAL.get(category, set())
        if action not in legal:
            raise ExceptionActionNotLegal(
                f"Action {action} is not legal for a {category} exception "
                f"(legal: {sorted(legal)})."
            )

        # An orphan statement line has no payment (A3 D1a keys it `<msgId>#<lineNo>`).
        is_line = (exc.get("subjectRef") or {}).get("kind") == "STATEMENT_LINE"
        if action == ACTION_DISMISS and category == CATEGORY_ORPHANED_SETTLEMENT:
            self._refuse_dismissing_a_claimable_line(exc)
        payment = None if is_line else self.payments.find_one({"paymentId": exc["paymentId"]})
        if payment is None and not is_line:
            raise ExceptionNotFound(
                f"Payment {exc['paymentId']} for exception {exception_id} not found."
            )

        if category == CATEGORY_RECONCILIATION_DISCREPANCY:
            # Parent plan Decision 2, enforced here and not only in the UI or the agent:
            # chargeBearer decides the books. DEBT → the bank absorbs the charge, so the only
            # closing entry is POST_ADJUSTMENT; any other bearer → ACCEPT, no entry (A4 D3).
            bearer = payment.get("chargeBearer")
            if action == ACTION_ACCEPT_DISCREPANCY and bearer == "DEBT":
                raise ExceptionActionNotLegal(
                    f"Payment {exc['paymentId']} has chargeBearer DEBT — the bank absorbs the "
                    "correspondent's charge, so close it with POST_ADJUSTMENT, not ACCEPT."
                )
            if action == ACTION_POST_ADJUSTMENT and bearer != "DEBT":
                raise ExceptionActionNotLegal(
                    f"Payment {exc['paymentId']} has chargeBearer {bearer} — the beneficiary "
                    "bears the charge, so no bank entry is due; close it with ACCEPT_DISCREPANCY."
                )

        now = datetime.now(timezone.utc)

        if action == ACTION_RETRY_SETTLEMENT:
            # Guard BEFORE the resolve write. `settle_payment` refuses a payment that is no
            # longer IN_PROGRESS, and since B5 requires resolving first there is no rollback:
            # a refusal would leave the exception RESOLVED with nothing having happened, and
            # the row would silently leave the queue. Check the precondition here so the
            # caller gets a 409 and the exception stays OPEN and retryable.
            retry_state = (payment.get("lifecycle") or {}).get("currentState")
            if retry_state != "IN_PROGRESS":
                raise ExceptionConflict(
                    f"Payment {exc['paymentId']} is {retry_state}, not OPEN to settlement — "
                    "a retry is only possible while the payment is IN_PROGRESS. The "
                    f"exception {exception_id} stays open."
                )
            # Resolve the held exception BEFORE re-driving settlement (B5). A DELAYED re-run
            # that lands DELAYED again calls record_exception, which dedupes on
            # (paymentId, category, OPEN) — if this row were still OPEN, the new occurrence
            # would collapse into it and then be marked RESOLVED by the tail, leaving the
            # payment stuck at IN_PROGRESS with no OPEN exception to re-trigger a retry.
            # Resolving first means a re-delay writes a FRESH occurrence (occurrence-per-doc,
            # B2), and a MATCHED re-run writes none.
            self._mark_exception_resolved(exc, action, note, now)
            # Re-drive settlement with the operator-chosen outcome. The payment is at
            # IN_PROGRESS (DELAYED holds there, not terminal), so settle_payment is a
            # legitimate forward transition. If it lands MATCHED → SETTLED.
            #
            # The guard above closes the common refusal, but the state can still move between
            # the check and here (the completion worker, a concurrent operator). Re-open the
            # exception on ANY failure rather than leaving it RESOLVED with nothing done —
            # a silently-closed row is the one failure mode the operator cannot see. The
            # re-open cannot collide with `idx_exception_open_unique`: this resolve holds the
            # only OPEN claim for (paymentId, category), and it closed it a moment ago.
            try:
                self.settle_payment(
                    exc["paymentId"], outcome=new_settlement_outcome or "MATCHED",
                )
            except Exception:
                try:
                    self.db["exceptions"].update_one(
                        {"_id": exc["_id"]},
                        {"$set": {
                            "status": STATUS_OPEN,
                            "resolution": None,
                            "updatedAt": datetime.now(timezone.utc),
                        }},
                    )
                except DuplicateKeyError:
                    # settle_payment wrote a fresh OPEN occurrence before raising, so it
                    # already holds the (paymentId, category) OPEN slot — leave ours closed
                    # and surface the original error, not the index collision.
                    logger.warning(
                        "resolve_exception: %s already has a fresh OPEN %s; not re-opening %s",
                        exc["paymentId"], category, exception_id,
                    )
                logger.warning(
                    "resolve_exception: RETRY_SETTLEMENT on %s failed — re-opened %s",
                    exc["paymentId"], exception_id, exc_info=True,
                )
                raise
        elif action == ACTION_RETURN_FUNDS:
            # return_of_funds flips the exception OPEN→RESOLVED *inside* its ACID txn
            # (conditional on OPEN) — the money move and the status claim are one atomic
            # operation, so a concurrent resolver or a double-click can never double-compensate
            # (B2). Do NOT run the generic flip here; return_of_funds owns it.
            return_of_funds(self._collections(), payment, exc, note=note)
        elif action == ACTION_ACCEPT_DISCREPANCY:
            # ACCEPT_DISCREPANCY moves no money — the resolution log is the only exception write.
            self._mark_exception_resolved(exc, action, note, now)
            # B1: a RECONCILIATION_DISCREPANCY exception lives on a SETTLED/POSTED payment that
            # the post-batch sweep re-checks every cycle (eligibility = currentState in
            # {SETTLED, POSTED} AND reconciliationStatus != RECONCILED). Accepting the
            # discrepancy without flipping the axis leaves the mismatch re-detectable, so
            # _stamp_discrepant opens a FRESH OPEN exception next batch — an infinite queue
            # loop where the only legal action (accept) never sticks. Flipping
            # reconciliationStatus to RECONCILED makes the operator's accept the final word on
            # this axis; the discrepancy itself is preserved in the exception's detail + the
            # resolution note (the cause, e.g. "correspondent fee"). The state advances to
            # RECONCILED too, exactly as the sweep's _stamp_reconciled does: flipping only the
            # axis left `currentState`/`status` at SETTLED, so the payments list (which reads
            # `status`) showed SETTLED while the lifecycle view showed RECONCILED.
            if category == CATEGORY_RECONCILIATION_DISCREPANCY:
                # ACCEPT never books anything (A4): a DEBT wire is refused above.
                self.payments.update_one(
                    {"paymentId": exc["paymentId"],
                     "lifecycle.currentState": {"$in": ["SETTLED", "POSTED"]}},
                    {"$set": {
                        "lifecycle.currentState": "RECONCILED",
                        "lifecycle.stateEnteredAt": now,
                        "lifecycle.reconciliationStatus": "RECONCILED",
                        "status": "RECONCILED",
                        "updatedAt": now,
                    }, "$push": {"lifecycle.events": {
                        "state": "RECONCILED", "at": now, "actor": "payments-operations",
                        "actorType": "USER",
                        "reason": f"Discrepancy accepted — {exc.get('exceptionId')}",
                    }}},
                )
                # Already past SETTLED/POSTED (e.g. the sweep got there first): axis only.
                self.payments.update_one(
                    {"paymentId": exc["paymentId"],
                     "lifecycle.reconciliationStatus": {"$ne": "RECONCILED"}},
                    {"$set": {"lifecycle.reconciliationStatus": "RECONCILED", "updatedAt": now}},
                )
        elif action == ACTION_POST_ADJUSTMENT:
            self._post_adjustment(payment, exc, note, now)
        elif action == ACTION_ESCALATE_TO_CORRESPONDENT:
            self._escalate(exc, note, now)
        else:  # ACTION_DISMISS — no money, no axis flip; the resolution log is the only write.
            self._mark_exception_resolved(exc, action, note, now)

        if payment is not None:
            verb = ("escalated to the correspondent" if action == ACTION_ESCALATE_TO_CORRESPONDENT
                    else "resolved")
            checks.append_checks(self.payments, payment["_id"], [
                checks.check(
                    "9 exceptions", "exception_resolved", checks.PASS,
                    mode=checks.SYNC,
                    detail=(
                        f"Exception {exception_id} ({category}) {verb} by payments-operations "
                        f"via {action}."
                    ),
                    actor="payments-operations", at=now,
                )
            ])

        updated = self.db["exceptions"].find_one({"exceptionId": exception_id})
        return updated

    def _post_adjustment(self, payment: dict, exc: dict, note: Optional[str], now: datetime) -> None:
        """Approve a DEBT wire's correspondent-charge correction (plan A4).

        One ACID txn: the conditional OPEN→RESOLVED claim first (B2 — a second resolver's
        claim matches 0 and aborts before anything is stamped), then the stamp the ledger's
        settlement_worker turns into Dr 5214 / Cr nostro, idempotent on {paymentId}-ADJ.
        `reconciliationStatus` is NOT flipped: reconciliation nets the posted -ADJ event and
        closes the leg itself (D2). `adjustmentPending` on the position holds leg 2 PENDING
        until that event exists, so the sweep cannot re-raise the discrepancy meanwhile.
        """
        adjustment = _settlement_adjustment_for(payment, exc)
        if adjustment is None:
            raise ExceptionActionNotLegal(
                f"Exception {exc.get('exceptionId')} carries no positive discrepancyAmount — "
                "there is no correspondent charge to book."
            )

        def callback(session) -> None:
            claim = self.db["exceptions"].find_one_and_update(
                {"_id": exc["_id"], "status": STATUS_OPEN},
                {"$set": {
                    "status": STATUS_RESOLVED,
                    "resolution": {"action": ACTION_POST_ADJUSTMENT, "by": "payments-operations",
                                   "at": now, "note": note},
                    "updatedAt": now,
                }},
                session=session,
            )
            if claim is None:
                raise ExceptionConflict(
                    f"Exception {exc.get('exceptionId')} is no longer OPEN — another resolver "
                    "already acted. Nothing was posted."
                )
            self.db["settlementPositions"].update_many(
                {"paymentId": payment["paymentId"]},
                {"$set": {"adjustmentPending": True}},
                session=session,
            )
            self.payments.update_one(
                {"paymentId": payment["paymentId"]},
                {"$set": {"clearing.settlementAdjustment": adjustment, "updatedAt": now}},
                session=session,
            )

        with self.db.client.start_session() as session:
            session.with_transaction(callback)

    def _refuse_dismissing_a_claimable_line(self, exc: dict) -> None:
        """A statement line some settled wire is still waiting for is linked or escalated,
        never dismissed — dismissing it strands the wire as MISSING (defect 2026-10-05, R2)."""
        subject = exc.get("subjectRef") or {}
        stmt = self.db["paymentMessages"].find_one(
            {"paymentMessageId": subject.get("paymentMessageId"), "purpose": "ACCOUNT_STATEMENT"},
            {"_id": 0, "statement.accountCode": 1, "entries": 1})
        line = next((e for e in (stmt or {}).get("entries") or []
                     if e.get("lineNo") == subject.get("lineNo")), None)
        if line is None:
            return
        amount = float(line.get("amount") or 0)
        waiting = list(self.db["settlementPositions"].find(
            {"settlementAccountCode": ((stmt or {}).get("statement") or {}).get("accountCode"),
             "actualAmount": None,
             "expectedAmount": {"$gte": amount - 0.005, "$lte": amount + 0.005}},
            {"_id": 0, "paymentId": 1}))
        for pos in waiting:
            lifecycle = (self.payments.find_one(
                {"paymentId": pos["paymentId"]}, {"lifecycle": 1}) or {}).get("lifecycle") or {}
            if (lifecycle.get("currentState") in ("SETTLED", "POSTED")
                    and lifecycle.get("reconciliationStatus") != "RECONCILED"):
                raise ExceptionConflict(
                    f"Line {subject.get('lineNo')} of {subject.get('paymentMessageId')} can be "
                    f"claimed by {pos['paymentId']}; link it or escalate, do not dismiss.")

    def _escalate(self, exc: dict, note: Optional[str], now: datetime) -> None:
        """Send the correspondent a camt.026 case and keep the exception OPEN (A4 D4).

        Claimed first (conditional on OPEN and not yet escalated), so a double-click sends
        one request; the message insert shares the claim's ACID txn.
        """
        from bson import ObjectId
        from contexts.financial_gateway.domain.inbound_documents import investigation_request_doc

        oid = ObjectId()
        message = investigation_request_doc(oid=oid, exception=exc, note=note, now=now)

        def callback(session) -> None:
            claim = self.db["exceptions"].find_one_and_update(
                {"_id": exc["_id"], "status": STATUS_OPEN, "awaitingCounterparty": {"$ne": True}},
                {"$set": {
                    "awaitingCounterparty": True,
                    "escalation": {"paymentMessageId": message["paymentMessageId"],
                                   "by": "payments-operations", "at": now, "note": note},
                    "updatedAt": now,
                }},
                session=session,
            )
            if claim is None:
                raise ExceptionConflict(
                    f"Exception {exc.get('exceptionId')} is already escalated or no longer OPEN."
                )
            self.payment_messages.insert_one(message, session=session)

        with self.db.client.start_session() as session:
            session.with_transaction(callback)

    def _mark_exception_resolved(
        self, exc: dict, action: str, note: Optional[str], now: datetime,
    ) -> None:
        """Flip an OPEN exception to RESOLVED/DISMISSED + stamp the resolution log.

        Conditional on ``status == OPEN`` so a concurrent resolver loses the race loudly
        (matched 0 → the caller's next read sees the other resolver's status) rather than
        silently double-writing. Used by every action except RETURN_FUNDS, whose flip lives
        inside return_of_funds' ACID txn (B2).
        """
        new_status = STATUS_DISMISSED if action == ACTION_DISMISS else STATUS_RESOLVED
        result = self.db["exceptions"].update_one(
            {"_id": exc["_id"], "status": STATUS_OPEN},
            {"$set": {
                "status": new_status,
                "resolution": {
                    "action": action,
                    "by": "payments-operations",
                    "at": now,
                    "note": note,
                },
                "updatedAt": now,
            }},
        )
        if result.matched_count == 0:
            raise ExceptionConflict(
                f"Exception {exc.get('exceptionId')} is no longer OPEN — another resolver "
                "already acted."
            )
