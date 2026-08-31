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
from datetime import date
from typing import Optional

from database.connection import MongoDBConnection
from process import payment_lifecycle
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
        self.payment_limit_usd = payment_limit_usd

    def _collections(self) -> PaymentCollections:
        return PaymentCollections(
            db=self.db,
            customers=self.customers,
            accounts=self.accounts,
            payments=self.payments,
            transactions=self.transactions,
            notifications=self.notifications,
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
        wire_details: Optional[dict] = None,
        ach_details: Optional[dict] = None,
        internal_details: Optional[dict] = None,
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
            idempotency_key=idempotency_key,
            wire_details=wire_details,
            ach_details=ach_details,
            internal_details=internal_details,
            collections=self._collections(),
            payment_limit_usd=self.payment_limit_usd,
        )
        return payment_lifecycle.run(ctx)

    def retrieve_payment(self, payment_ref: str) -> Optional[dict]:
        """Retrieve a payment plus its single transaction doc (v4_21)."""
        payment = self.payments.find_one({"paymentId": payment_ref})
        if not payment:
            return None
        payment["_txn"] = self.transactions.find_one({"paymentId": payment_ref})
        return payment
