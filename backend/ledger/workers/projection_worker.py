"""Projection worker — Stage ②: ledgerEvents → subLedgerEntries.

Change stream on `ledgerEvents` inserts. For each event, fans out into two
subLedgerEntries (DEBIT + CREDIT) and stamps ledgerEvents.postingResult.
Idempotent via unique idempotencyKey per sub-ledger entry.

Resume token persisted in `ledgerStreamTokens`.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from database.connection import MongoDBConnection
from services.subledger_service import build_subledger_entries, write_subledger_entries
from shared.coa_cache import ChartOfAccounts

logger = logging.getLogger(__name__)

WORKER_ID = "projection_worker"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load_resume_token(connection: MongoDBConnection, db_name: str) -> dict | None:
    doc = connection.get_collection(db_name, "ledgerStreamTokens").find_one({"workerId": WORKER_ID})
    return doc.get("resumeToken") if doc else None


def _save_resume_token(connection: MongoDBConnection, db_name: str, token: dict) -> None:
    connection.get_collection(db_name, "ledgerStreamTokens").update_one(
        {"workerId": WORKER_ID},
        {"$set": {"resumeToken": token, "updatedAt": _now_utc()}},
        upsert=True,
    )


def _mark_failed(event_id: str, reason: str, connection: MongoDBConnection, db_name: str) -> None:
    connection.get_collection(db_name, "ledgerEvents").update_one(
        {"eventId": event_id},
        {"$set": {
            "postingStatus": "FAILED",
            "postingResult.failureReason": reason,
            "postingResult.failedAt": _now_utc(),
        }},
    )
    logger.warning("ledgerEvent %s marked FAILED: %s", event_id, reason)


def process_ledger_event(
    event: dict,
    connection: MongoDBConnection,
    db_name: str,
    coa: ChartOfAccounts,
) -> None:
    event_id = event.get("eventId")
    if not event_id:
        logger.warning("ledgerEvent missing eventId; skipping")
        return

    if "debitLeg" not in event or "creditLeg" not in event:
        _mark_failed(event_id, "missing debitLeg or creditLeg", connection, db_name)
        return

    try:
        # Gap 5: re-verify balance at projection time (design step 3.3)
        debit_amount = event["debitLeg"].get("amount")
        credit_amount = event["creditLeg"].get("amount")
        if debit_amount != credit_amount:
            raise ValueError(
                f"amount mismatch: debit={debit_amount} != credit={credit_amount}"
            )

        # Gap 6: validate GL accounts are ACTIVE posting leaves
        for side, leg_key in [("DEBIT", "debitLeg"), ("CREDIT", "creditLeg")]:
            account_code = event[leg_key].get("glAccountCode")
            acct = coa.require_active_posting_account(account_code)
            # normalBalance mismatch warning removed: liability accounts (normalBalance=CREDIT)
            # are correctly debited on outgoing transfers — fires on 100% of payments, no signal.
            _ = acct

        entries = build_subledger_entries(event)
        write_subledger_entries(entries, event_id, connection, db_name)
    except ValueError as e:
        _mark_failed(event_id, str(e), connection, db_name)


def run(connection: MongoDBConnection, db_name: str, coa: ChartOfAccounts) -> None:
    logger.info("projection_worker starting — watching ledgerEvents on %s", db_name)
    resume_token = _load_resume_token(connection, db_name)

    ledger_events = connection.get_collection(db_name, "ledgerEvents")
    pipeline = [{"$match": {"operationType": "insert"}}]
    kwargs: dict = {"resume_after": resume_token} if resume_token else {}

    with ledger_events.watch(pipeline, **kwargs) as stream:
        for change in stream:
            event = change.get("fullDocument", {})
            try:
                process_ledger_event(event, connection, db_name, coa)
                _save_resume_token(connection, db_name, change["_id"])
            except Exception:
                logger.exception("error projecting eventId=%s", event.get("eventId"))
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
            logger.exception("projection_worker crashed; restarting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
