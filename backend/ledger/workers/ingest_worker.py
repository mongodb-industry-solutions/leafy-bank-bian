"""Ingest worker — Stage ①: transactions → ledgerEvents.

Change stream on `transactions` inserts. For each settled payment, decomposes it
into a one-doc ledgerEvent with debitLeg + creditLeg and writes it to `ledgerEvents`.
Idempotent via unique idempotencyKey (= paymentId).

Resume token persisted in `changeStreamTokens` so a restart replays nothing and
misses nothing.
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
from shared.coa_cache import ChartOfAccounts
from shared.posting_rules import MAPPING_VERSION, SIDE_CREDIT, SIDE_DEBIT, decompose_principal_payment
from shared.refs import PREFIX_GROUP, PREFIX_LEDGER_EVENT, derive_ref

logger = logging.getLogger(__name__)

WORKER_ID = "ingest_worker"

# BIAN SourceSystemReference / SourceTransactionType constants for ledgerEvents.
_SOURCE_SYSTEM = "LEDGER_PIPELINE"
_SOURCE_TYPE_PAYMENT = "PAYMENT"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _derive_posting_mode(rail: str | None, payment_type: str | None) -> str:
    rail = (rail or "").upper()
    ptype = (payment_type or "").upper()
    if rail in ("WIRE", "INTERNAL") or "WIRE" in ptype:
        return "REALTIME"
    return "BATCH"  # ACH, VENMO, PAYPAL, and any other rail settle batch by default


def build_ledger_event(
    txn: dict,
    payer_account: dict,
    payee_account: dict,
    coa: ChartOfAccounts,
) -> dict:
    """Pure: assemble the one-doc ledgerEvent from a transaction and its two accounts."""
    legs = decompose_principal_payment(
        amount=txn["amount"],
        currency=txn.get("currency", "USD"),
        debtor_account=payer_account,
        creditor_account=payee_account,
        coa=coa,
    )
    debit_leg = next(l for l in legs if l.side == SIDE_DEBIT)
    credit_leg = next(l for l in legs if l.side == SIDE_CREDIT)

    oid = ObjectId()
    payment_id = txn["paymentId"]
    occurred_at = txn.get("settledAt") or txn.get("updatedAt") or _now_utc()
    if isinstance(occurred_at, str):
        occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    period_code = occurred_at.strftime("%Y-%m")

    return {
        "_id": oid,
        "eventId": derive_ref(PREFIX_LEDGER_EVENT, oid),
        "idempotencyKey": payment_id,
        "groupId": derive_ref(PREFIX_GROUP, oid),
        "occurredAt": occurred_at,
        "valueDate": occurred_at,
        "periodName": occurred_at.strftime("%B %Y"),
        "description": f"{debit_leg.event_type}: {payment_id}",
        "meta": {
            "subLedgerType": "CUSTOMER_DEPOSITS",
            "periodCode": period_code,
            "sourceSystem": _SOURCE_SYSTEM,
        },
        "eventType": debit_leg.event_type,
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
            "sourceCollection": "transactions",
            "sourceId": payment_id,
            "sourceSystem": _SOURCE_SYSTEM,
            "sourceType": _SOURCE_TYPE_PAYMENT,
        },
        "rail": txn.get("rail"),
        "paymentType": txn.get("paymentType"),
        "postingMode": {"type": _derive_posting_mode(txn.get("rail"), txn.get("paymentType"))},
        "reversalOf": None,
        "postingStatus": "PENDING",
        "postingResult": None,
        "mappingVersion": MAPPING_VERSION,
        "createdAt": _now_utc(),
    }


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


def process_transaction(
    txn: dict,
    connection: MongoDBConnection,
    db_name: str,
    coa: ChartOfAccounts,
) -> None:
    """Fetch accounts, build and insert the ledgerEvent for one transaction."""
    payment_id = txn.get("paymentId")
    if not payment_id:
        logger.warning("transaction missing paymentId; skipping")
        return

    payer_account_id = (txn.get("payer") or {}).get("accountId")
    payee_account_id = (txn.get("payee") or {}).get("accountId")
    if not payer_account_id or not payee_account_id:
        logger.warning("transaction %s missing payer/payee accountId; skipping", payment_id)
        return

    accounts_coll = connection.get_collection(db_name, "accounts")
    payer_account = accounts_coll.find_one({"accountId": payer_account_id})
    payee_account = accounts_coll.find_one({"accountId": payee_account_id})

    if payer_account is None:
        raise ValueError(f"payer account {payer_account_id!r} not found")
    if payee_account is None:
        raise ValueError(f"payee account {payee_account_id!r} not found")

    event = build_ledger_event(txn, payer_account, payee_account, coa)
    try:
        connection.get_collection(db_name, "ledgerEvents").insert_one(event)
        logger.info("ledgerEvent %s created for paymentId=%s", event["eventId"], payment_id)
    except DuplicateKeyError:
        logger.info("ledgerEvent already exists for paymentId=%s; skipping", payment_id)


def run(connection: MongoDBConnection, db_name: str, coa: ChartOfAccounts) -> None:
    logger.info("ingest_worker starting — watching transactions on %s", db_name)
    resume_token = _load_resume_token(connection, db_name)

    transactions = connection.get_collection(db_name, "transactions")
    pipeline = [{"$match": {"operationType": "insert"}}]
    kwargs: dict = {"resume_after": resume_token} if resume_token else {}

    try:
        stream_cm = transactions.watch(pipeline, **kwargs)
    except OperationFailure as exc:
        if exc.has_error_label("NonResumableChangeStreamError") and resume_token is not None:
            logger.warning(
                "resume token no longer in oplog (%s); clearing and starting a fresh stream",
                exc.details.get("codeName") if exc.details else exc,
            )
            _clear_resume_token(connection, db_name)
            stream_cm = transactions.watch(pipeline)
        else:
            raise

    with stream_cm as stream:
        for change in stream:
            txn = change.get("fullDocument", {})
            try:
                process_transaction(txn, connection, db_name, coa)
                _save_resume_token(connection, db_name, change["_id"])
            except Exception:
                logger.exception("error processing paymentId=%s", txn.get("paymentId"))
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
            logger.exception("ingest_worker crashed; restarting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
