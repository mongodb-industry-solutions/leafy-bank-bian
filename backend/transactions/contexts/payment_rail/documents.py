"""Pure document builders for the execution stage. No I/O, no clock.

Moved verbatim from `services/payments_service.py`. BIAN PaymentRail (SD 47741).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from bson import ObjectId

from contexts.payment_order_initiation.domain.bank_identity import (
    OUR_BANK_COUNTRY,
    OUR_BIC,
)
from shared.refs import derive_ref


def transaction_doc(
    *,
    payment_oid: ObjectId,
    payment_id: str,
    debtor_account: dict,
    debtor_customer: dict,
    creditor_account: dict,
    creditor_customer: dict,
    debtor_after: dict,
    amount: float,
    currency: str,
    payment_rail: str,
    txn_code: str,
    is_internal: bool,
    now: datetime,
    payment_execution_id: Optional[str] = None,
    fee_amount: float = 0.0,
    fee_currency: Optional[str] = None,
) -> dict:
    """One v4_21 transactions doc: the confirmed payer->payee movement. NOT an accounting record
    (no legs, no gl) — the ledger service derives DR/CR ledgerEvents from this via CDC."""
    debtor_name = (debtor_customer.get("identification") or {}).get("legalName")
    # An external creditor has no customer record — the name lives in the payment's payee
    # snapshot, not in a `customers` lookup. The clearing account (stage 7 B1) is the
    # `creditor_account` for an external wire, and `creditor_customer` is None.
    creditor_name = ((creditor_customer or {}).get("identification") or {}).get("legalName")
    return {
        "_id": ObjectId(),
        "txnId": derive_ref("TXN", payment_oid),
        "paymentId": payment_id,
        "bankRef": f"LEAFY-BOOK-{payment_id.split('-', 1)[-1]}",
        "rail": payment_rail,
        "paymentType": "CREDIT_TRANSFER",
        "direction": "OUTGOING",
        "txnCode": txn_code,
        "amount": amount,
        "currency": currency,
        "baseAmount": amount,
        "valueDate": now.date().isoformat(),
        "bookingDate": now.date().isoformat(),
        "description": f"Transfer to {creditor_account.get('accountNumber')}",
        "balanceAfter": (debtor_after.get("balance", {}) or {}).get("current"),
        "channel": "API",
        "payer": {
            "accountId": debtor_account["accountId"],
            "accountNo": debtor_account.get("accountNumber"),
            "name": debtor_name,
            "bic": OUR_BIC,
            "country": OUR_BANK_COUNTRY,
            "isInternal": True,
        },
        "payee": {
            "accountId": creditor_account["accountId"],
            "accountNo": creditor_account.get("accountNumber"),
            "name": creditor_name,
            "bic": OUR_BIC,
            "country": OUR_BANK_COUNTRY,
            "isInternal": is_internal,
        },
        "transactionCategory": "AccountTransfer",
        "isReversed": False,
        "reversalTxnId": None,
        "transactionDates": [
            {"date": now, "type": "TransactionInitiatedDate"},
            {"date": now, "type": "TransactionCompletedDate"},
        ],
        "transactionStatus": "Completed",
        "isCompleted": True,
        "isNotified": False,
        "createdAt": now,
        "createdBy": "SERVICE-PAYMENTS",
        "sourceSystem": "leafy-bank-payments-service",
        # Stage 5 (doc 19 B6). Her L868 lists `paymentExecutionId` on this record; the
        # canonical spec's `transactions` collection does **not** declare it (0 matches
        # spec-wide), so it is a non-spec field pending ratification — Q37.
        #
        # ⚠️ **This is the stage's only boundary change, and it is additive only.** The
        # ledger's `ingest_worker` reads `amount, paymentId, currency, payer{accountId},
        # payee{accountId}, paymentType, rail, settledAt, updatedAt, sourceSystem` (doc 12
        # §1) and ignores everything else. Nothing above is renamed, retyped or removed.
        #
        # Null on a book transfer, which writes no execution artifact (doc 19 B4) — so today
        # it is null in practice and non-null the moment a rail-bound payment can also settle
        # (stage 7). Written now because the boundary document is the one place where a later
        # edit is expensive.
        "paymentExecutionId": payment_execution_id,
        # Stage 6 (doc 20 B3(a)). The charge stage 3 recorded on `payments.fees[]`, carried
        # across the boundary so the ledger can post a fee leg without reading `payments`
        # (B1 forbids the derivation path from reading it).
        #
        # ⚠️ **Additive only, exactly like `paymentExecutionId` above.** In particular
        # `amount` is untouched: `enrichment_plan.py:264` — *"`amount` must not change. It is
        # the settlement amount and the ledger's primary input"* — so a fee is an ADDITIONAL
        # balanced leg pair, never a deduction from the principal.
        #
        # 0.0 on an internal transfer: stage 3 levies a charge on `rail == "WIRE"` only.
        "feeAmount": fee_amount,
        "feeCurrency": fee_currency or currency,
    }


def build_notifications(
    *,
    payment_oid: ObjectId,
    payment_id: str,
    txn_id: str,
    debtor_account: dict,
    creditor_account: dict,
    debtor_customer: dict,
    debtor_after: dict,
    amount: float,
    currency: str,
    payment_rail: str,
    is_internal: bool,
    now: datetime,
) -> list[dict]:
    """Build the sender-side notification for a payment.

    Leafy Bank UX: only the debtor (sender) receives a notification. Always returns exactly
    one document. `txn_id` is the single v4_21 transaction doc's txnId.
    """
    debtor_balance = (debtor_after.get("balance", {}) or {}).get("current")
    creditor_name = creditor_account.get("accountNumber") or creditor_account.get("accountId")

    if is_internal:
        event_type = "InternalTransfer"
        message = (
            f"You transferred {currency} {amount} between your accounts. "
            f"New balance on {debtor_account['accountId']}: {currency} {debtor_balance}."
        )
    elif payment_rail == "INTERNAL":
        event_type = "TransferSent"
        message = (
            f"You sent {currency} {amount} to {creditor_name}. "
            f"New balance: {currency} {debtor_balance}."
        )
    else:
        event_type = "PaymentMade"
        message = (
            f"You paid {currency} {amount} to {creditor_name}. "
            f"New balance: {currency} {debtor_balance}."
        )

    notif_oid = ObjectId()
    return [
        {
            "_id": notif_oid,
            "notificationId": derive_ref("NOTIF", notif_oid),
            "eventType": event_type,
            "message": message,
            "notificationDate": now,
            "recipient": {"customerId": debtor_customer["customerId"]},
            "transactionId": txn_id,
            "paymentId": payment_id,
            "accounts": {
                "senderAccountId": debtor_account["accountId"],
                "receiverAccountId": creditor_account["accountId"],
            },
            "createdAt": now,
            "createdBy": "SERVICE-PAYMENTS",
            "sourceSystem": "leafy-bank-payments-service",
        }
    ]
