"""Settlement worker — Stage 7: payments settlement transition → ledgerEvents.

Change stream on `payments` updates. When a payment's `lifecycle.settlementStatus` advances
to `SETTLED`, this worker produces the **second** `ledgerEvents` document — the settlement
accounting event (doc 21 B2):

    Dr 1131 Wire Clearing       (clearing position reduced)
    Cr 1111 Nostro Accounts      (model 1: via correspondent)
       or Cr 1121 Minimum Reserve Requirements (model 2: direct to central bank)

The first event (PAYMENT_PRINCIPAL, ``Dr customer / Cr 1131``) is produced by
``ingest_worker`` from the `transactions` insert. This worker produces the second from the
`payments` settlement transition — the two events are independent accounting facts sourced
from different collections, exactly as decisions.md 2026-06-18 specifies (async CDC, never
inside the money-move transaction).

Idempotent via unique `idempotencyKey = "{paymentId}-SETTLEMENT"`.
Resume token persisted in `changeStreamTokens` (same collection as `ingest_worker`, distinct
`workerId`).

⚠️ **`full_document="updateLookup"` is required** — unlike `ingest_worker` which watches
inserts (where `fullDocument` is always present), this worker watches updates. Without
`updateLookup`, `fullDocument` is `None` for update events and there is nothing to read.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from bson import ObjectId
from dotenv import load_dotenv
from pymongo.errors import DuplicateKeyError, OperationFailure

from database.connection import MongoDBConnection
from shared.change_stream import iter_with_idle_checkpoint
from shared.coa_cache import ChartOfAccounts
from shared.posting_rules import (
    MAPPING_VERSION,
    EVENT_PAYMENT_SETTLEMENT,
    EVENT_SETTLEMENT_ADJUSTMENT,
    SIDE_CREDIT,
    SIDE_DEBIT,
    decompose_inbound_settlement,
    decompose_settlement,
    decompose_settlement_adjustment,
)
from shared.refs import PREFIX_GROUP, PREFIX_LEDGER_EVENT, derive_ref

logger = logging.getLogger(__name__)

WORKER_ID = "settlement_worker"

_SOURCE_SYSTEM = "LEDGER_PIPELINE"
_SOURCE_TYPE_SETTLEMENT = "SETTLEMENT"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load_resume_token(connection: MongoDBConnection, db_name: str) -> dict | None:
    doc = connection.get_collection(db_name, "changeStreamTokens").find_one({"workerId": WORKER_ID})
    return doc.get("resumeToken") if doc else None


def _save_resume_token(connection: MongoDBConnection, db_name: str, token: dict) -> None:
    connection.get_collection(db_name, "changeStreamTokens").update_one(
        {"workerId": WORKER_ID},
        {"$set": {"resumeToken": token, "updatedAt": _now_utc()}},
        upsert=True,
    )


def _clear_resume_token(connection: MongoDBConnection, db_name: str) -> None:
    connection.get_collection(db_name, "changeStreamTokens").delete_one({"workerId": WORKER_ID})


def build_settlement_event(
    payment: dict,
    txn: dict,
    clearing_account: dict,
    coa: ChartOfAccounts,
) -> dict:
    """Pure: assemble the settlement ledgerEvent from a settled payment and its clearing account.

    The ``settlementAccountCode`` (1111 or 1121) is read from ``payment.clearing`` — the
    settlement stage stamps it at the same moment as the SETTLED transition (outbound
    ``settle.py``; inbound ``apply.py``), so the worker never sees a SETTLED payment without
    it. The leg AMOUNT comes from ``txn`` (the clearing amount moved at stage 6), not
    ``payment.amount`` — see the note at the call below.

    ## Direction: the two events are mirror images (FR-7.IN1)

    Outbound (money leaves us): ``Dr 1131 Wire Clearing / Cr 1111|1121`` — the hold on the
    clearing account is released to the settlement account.
    Inbound (money arrived): ``Dr 1111|1121 Nostro / Cr 1131 Wire Clearing`` — the nostro
    position funds the release of the hold stage 6 took. Doina's FR-7.IN1: *"the mirror of
    outgoing FR-7.1."* Both directions net the clearing account to zero.
    """
    settlement_code = (payment.get("clearing") or {}).get("settlementAccountCode")
    if not settlement_code:
        raise ValueError(
            f"payment {payment.get('paymentId')!r} has no clearing.settlementAccountCode — "
            "the settle stage must stamp it at the SETTLED transition "
            "(settle.py outbound, apply.py inbound)"
        )

    inbound = payment.get("direction") == "INBOUND"
    decompose = decompose_inbound_settlement if inbound else decompose_settlement

    # Source the leg amount from the `transactions` doc (the clearing amount actually
    # moved at stage 6), NOT `payment.amount` — which FX diverges to the converted amount.
    # Using txn.amount here makes the settlement event's clearing leg equal the principal
    # event's clearing leg so the clearing account nets to zero; the FX exchange is
    # recorded on `settlementPositions` instead (FR-7.6).
    legs = decompose(
        amount=txn.get("amount", 0),
        currency=txn.get("currency", "USD"),
        clearing_account=clearing_account,
        settlement_account_code=settlement_code,
        coa=coa,
    )

    debit_leg = next(l for l in legs if l.side == SIDE_DEBIT)
    credit_leg = next(l for l in legs if l.side == SIDE_CREDIT)

    payment_id = payment["paymentId"]
    oid = ObjectId()
    occurred_at = _now_utc()
    # `clearing.settledAt` is stamped by settle.py as an ISO string; fall back to now.
    settled_at = (payment.get("clearing") or {}).get("settledAt")
    if isinstance(settled_at, str):
        occurred_at = datetime.fromisoformat(settled_at.replace("Z", "+00:00"))

    period_code = occurred_at.strftime("%Y-%m")

    return {
        "_id": oid,
        "eventId": derive_ref(PREFIX_LEDGER_EVENT, oid),
        "idempotencyKey": f"{payment_id}-SETTLEMENT",
        "groupId": derive_ref(PREFIX_GROUP, oid),
        "occurredAt": occurred_at,
        "valueDate": occurred_at,
        "periodName": occurred_at.strftime("%B %Y"),
        "description": (
            f"{'Inbound' if inbound else 'External'} settlement posting — "
            f"SETTLEMENT: {payment_id}"
        ),
        "meta": {
            "subLedgerType": "CLEARING_AND_SETTLEMENT",
            "periodCode": period_code,
            "sourceSystem": _SOURCE_SYSTEM,
        },
        "eventType": EVENT_PAYMENT_SETTLEMENT,
        "debitLeg": {
            "glAccountCode": debit_leg.gl_account_code,
            "controlAccountCode": coa.control_account_for(debit_leg.gl_account_code),
            "amount": debit_leg.amount_minor,
            "currency": debit_leg.currency,
            "functionalAmount": debit_leg.amount_minor,
            "entityReference": {
                "entityType": "ACCOUNT",
                "entityId": debit_leg.account_id,
            },
        },
        "creditLeg": {
            "glAccountCode": credit_leg.gl_account_code,
            "controlAccountCode": coa.control_account_for(credit_leg.gl_account_code),
            "amount": credit_leg.amount_minor,
            "currency": credit_leg.currency,
            "functionalAmount": credit_leg.amount_minor,
            "entityReference": {
                "entityType": "ACCOUNT",
                "entityId": credit_leg.account_id,
            },
        },
        "sourceReference": {
            "sourceCollection": "payments",
            "sourceId": payment_id,
            "sourceSystem": _SOURCE_SYSTEM,
            "sourceType": _SOURCE_TYPE_SETTLEMENT,
        },
        "rail": payment.get("rail") or payment.get("paymentType"),
        "paymentType": payment.get("paymentType"),
        "postingMode": {"type": "BATCH"},
        "reversalOf": None,
        "postingStatus": "PENDING",
        "postingResult": None,
        "mappingVersion": MAPPING_VERSION,
        "createdAt": _now_utc(),
    }


def build_adjustment_event(
    payment: dict,
    clearing_account: dict,
    coa: ChartOfAccounts,
) -> dict | None:
    """Pure: the operator-approved short-pay correction, or None when there is none to post.

    Stage 9's ACCEPT_DISCREPANCY stamps ``clearing.settlementAdjustment`` on a DEBT wire the
    rail settled short (Sep 17 L1264-1286). This worker already sees that update — the
    payment is SETTLED — so the correction is derived here, keeping ledgerEvents a
    ledger-service write (the async-CDC firewall). Idempotent on ``{paymentId}-ADJ``.
    """
    adj = (payment.get("clearing") or {}).get("settlementAdjustment")
    settlement_code = (payment.get("clearing") or {}).get("settlementAccountCode")
    if not adj or not settlement_code:
        return None
    legs = decompose_settlement_adjustment(
        amount=adj.get("amount", 0),
        currency=adj.get("currency", "USD"),
        clearing_account=clearing_account,
        settlement_account_code=settlement_code,
        coa=coa,
    )
    if not legs:
        return None
    debit_leg = next(l for l in legs if l.side == SIDE_DEBIT)
    credit_leg = next(l for l in legs if l.side == SIDE_CREDIT)

    payment_id = payment["paymentId"]
    oid = ObjectId()
    occurred_at = _now_utc()

    def _leg(leg) -> dict:
        return {
            "glAccountCode": leg.gl_account_code,
            "controlAccountCode": coa.control_account_for(leg.gl_account_code),
            "amount": leg.amount_minor,
            "currency": leg.currency,
            "functionalAmount": leg.amount_minor,
            "entityReference": {"entityType": "ACCOUNT", "entityId": leg.account_id},
        }

    return {
        "_id": oid,
        "eventId": derive_ref(PREFIX_LEDGER_EVENT, oid),
        "idempotencyKey": f"{payment_id}-ADJ",
        "groupId": derive_ref(PREFIX_GROUP, oid),
        "occurredAt": occurred_at,
        "valueDate": occurred_at,
        "periodName": occurred_at.strftime("%B %Y"),
        "description": (
            f"Correspondent charge absorbed (chargeBearer DEBT) — approved on "
            f"{adj.get('exceptionId')}: {payment_id}"
        ),
        "meta": {
            "subLedgerType": "CLEARING_AND_SETTLEMENT",
            "periodCode": occurred_at.strftime("%Y-%m"),
            "sourceSystem": _SOURCE_SYSTEM,
        },
        "eventType": EVENT_SETTLEMENT_ADJUSTMENT,
        "debitLeg": _leg(debit_leg),
        "creditLeg": _leg(credit_leg),
        "sourceReference": {
            "sourceCollection": "exceptions",
            "sourceId": adj.get("exceptionId") or payment_id,
            "sourceSystem": _SOURCE_SYSTEM,
            "sourceType": EVENT_SETTLEMENT_ADJUSTMENT,
        },
        "rail": payment.get("rail") or payment.get("paymentType"),
        "paymentType": payment.get("paymentType"),
        "postingMode": {"type": "BATCH"},
        "reversalOf": None,
        "postingStatus": "PENDING",
        "postingResult": None,
        "mappingVersion": MAPPING_VERSION,
        "createdAt": _now_utc(),
    }


def process_settlement(
    payment: dict,
    connection: MongoDBConnection,
    db_name: str,
    coa: ChartOfAccounts,
) -> None:
    """Build and insert the settlement ledgerEvent for one settled payment."""
    payment_id = payment.get("paymentId")
    if not payment_id:
        raise ValueError("payment missing paymentId")

    # Find the clearing account from the transactions doc — by DIRECTION, because the two
    # directions put the clearing account on opposite sides of the same doc (her L1036: the
    # inbound difference is only "the direction of the debit/credit legs"):
    #   outbound external wire: payer = the customer, payee = the clearing account
    #   inbound wire:           payer = the clearing account, payee = the customer
    # Reading `payee` unconditionally resolves the CUSTOMER on an inbound payment, whose
    # gl.accountCode is a deposit control — the settlement event would debit the customer's
    # deposit instead of the nostro. The `payer`/`payee` axis and `direction` are orthogonal
    # (an internal transfer's payee is a customer too); direction is what decides which side
    # the clearing account sits on.
    transactions = connection.get_collection(db_name, "transactions")
    txn = transactions.find_one({"paymentId": payment_id})
    if not txn:
        raise ValueError(f"no transactions doc for paymentId={payment_id} — cannot find clearing account")

    side = "payer" if payment.get("direction") == "INBOUND" else "payee"
    clearing_account_id = (txn.get(side) or {}).get("accountId")
    if not clearing_account_id:
        raise ValueError(
            f"transactions doc for paymentId={payment_id} has no {side}.accountId"
        )

    accounts = connection.get_collection(db_name, "accounts")
    clearing_account = accounts.find_one({"accountId": clearing_account_id})
    if not clearing_account:
        raise ValueError(f"clearing account {clearing_account_id!r} not found in accounts")

    event = build_settlement_event(payment, txn, clearing_account, coa)
    le_coll = connection.get_collection(db_name, "ledgerEvents")
    try:
        le_coll.insert_one(event)
        logger.info(
            "settlement ledgerEvent %s created for paymentId=%s (Dr %s / Cr %s, %s minor units)",
            event["eventId"], payment_id,
            event["debitLeg"]["glAccountCode"], event["creditLeg"]["glAccountCode"],
            event["debitLeg"]["amount"],
        )
    except DuplicateKeyError:
        logger.info("settlement ledgerEvent already exists for paymentId=%s; skipping", payment_id)

    # Every later update to this SETTLED payment re-delivers it here; the approved
    # short-pay correction rides on one of those (stage 9 accept).
    adjustment = build_adjustment_event(payment, clearing_account, coa)
    if adjustment is not None:
        try:
            le_coll.insert_one(adjustment)
            logger.info(
                "settlement adjustment ledgerEvent %s created for paymentId=%s (Dr %s / Cr %s)",
                adjustment["eventId"], payment_id,
                adjustment["debitLeg"]["glAccountCode"], adjustment["creditLeg"]["glAccountCode"],
            )
        except DuplicateKeyError:
            pass
        # Plan A4 D2 — POST_ADJUSTMENT set this on the position so reconciliation waits for
        # the event instead of re-raising the discrepancy; the event now exists.
        connection.get_collection(db_name, "settlementPositions").update_many(
            {"paymentId": payment_id, "adjustmentPending": True},
            {"$set": {"adjustmentPending": False}},
        )


def run(connection: MongoDBConnection, db_name: str, coa: ChartOfAccounts) -> None:
    logger.info("settlement_worker starting — watching payments on %s", db_name)
    payments = connection.get_collection(db_name, "payments")
    pipeline = [
        {
            "$match": {
                "operationType": "update",
                "fullDocument.lifecycle.settlementStatus": "SETTLED",
            }
        }
    ]

    while True:
        resume_token = _load_resume_token(connection, db_name)
        kwargs: dict = {"resume_after": resume_token, "full_document": "updateLookup"} if resume_token else {"full_document": "updateLookup"}
        try:
            with payments.watch(pipeline, **kwargs) as stream:
                checkpoint = lambda token: _save_resume_token(connection, db_name, token)  # noqa: E731
                for change in iter_with_idle_checkpoint(stream, checkpoint):
                    payment = change.get("fullDocument", {})
                    if not payment:
                        logger.warning("settlement_worker: change event has no fullDocument — skipping")
                        _save_resume_token(connection, db_name, change["_id"])
                        continue
                    try:
                        process_settlement(payment, connection, db_name, coa)
                        _save_resume_token(connection, db_name, change["_id"])
                    except Exception:
                        logger.exception("error processing settlement for paymentId=%s", payment.get("paymentId"))
                        raise
        except OperationFailure as exc:
            if exc.has_error_label("NonResumableChangeStreamError") and resume_token is not None:
                logger.critical(
                    "settlement_worker: resume token no longer in oplog (%s); clearing and "
                    "starting a fresh stream — any payments settled since the last saved token "
                    "were NOT ingested into ledgerEvents and will not be retried.",
                    exc.details.get("codeName") if exc.details else exc,
                )
                _clear_resume_token(connection, db_name)
                continue
            raise


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI is not set")
    db_name = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")

    connection = MongoDBConnection(uri)
    coa = ChartOfAccounts.from_db(connection, db_name)
    logger.info("CoA loaded: %d accounts", len(coa))

    while True:
        try:
            run(connection, db_name, coa)
        except Exception:
            logger.exception("settlement_worker crashed; restarting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
