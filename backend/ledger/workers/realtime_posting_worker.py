"""Realtime posting worker — Stage ③ (realtime): ledgerEvents → journalEntries.

Change stream on `ledgerEvents` updates. Fires when projection_worker commits
Stage ② (postingResult stamped, both subLedgerEntries written) for an event whose
postingMode is REALTIME or NEAR_REALTIME. Writes ONE per-transaction journal
immediately, flipping the event PENDING → POSTED — no wait for the batch window.

BATCH events are left for gl_batch, which summarizes many transactions per window.
Idempotent via journalEntries.idempotencyKey. Resume token in `changeStreamTokens`.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo.errors import DuplicateKeyError, OperationFailure

from database.connection import MongoDBConnection
from services.journal_service import post_realtime_event
from shared.coa_cache import ChartOfAccounts

logger = logging.getLogger(__name__)

WORKER_ID = "realtime_posting_worker"


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


# Match events that just completed Stage ② (subledger legs written, postingResult
# stamped) and are realtime/near-realtime and not yet posted to the GL.
_MATCH = {
    "$match": {
        "operationType": "update",
        "fullDocument.postingMode.type": {"$in": ["REALTIME", "NEAR_REALTIME"]},
        "fullDocument.postingStatus": "PENDING",
        "fullDocument.postingResult.subLedgerIdDebit": {"$exists": True},
    }
}


def process_event(
    event: dict,
    connection: MongoDBConnection,
    db_name: str,
    coa: ChartOfAccounts,
) -> None:
    event_id = event.get("eventId")
    if not event_id:
        logger.warning("ledgerEvent missing eventId; skipping realtime post")
        return
    try:
        post_realtime_event(event_id, connection, db_name, coa=coa)
    except DuplicateKeyError:
        logger.info("realtime journal already exists for eventId=%s; skipping", event_id)


def run(connection: MongoDBConnection, db_name: str, coa: ChartOfAccounts) -> None:
    logger.info("realtime_posting_worker starting — watching ledgerEvents on %s", db_name)
    resume_token = _load_resume_token(connection, db_name)

    ledger_events = connection.get_collection(db_name, "ledgerEvents")
    pipeline = [_MATCH]
    kwargs: dict = {"full_document": "updateLookup"}
    if resume_token:
        kwargs["resume_after"] = resume_token

    try:
        stream_cm = ledger_events.watch(pipeline, **kwargs)
    except OperationFailure as exc:
        if exc.has_error_label("NonResumableChangeStreamError") and resume_token is not None:
            logger.warning(
                "resume token no longer in oplog (%s); clearing and starting a fresh stream",
                exc.details.get("codeName") if exc.details else exc,
            )
            _clear_resume_token(connection, db_name)
            stream_cm = ledger_events.watch([_MATCH], full_document="updateLookup")
        else:
            raise

    with stream_cm as stream:
        for change in stream:
            event = change.get("fullDocument", {})
            try:
                process_event(event, connection, db_name, coa)
                _save_resume_token(connection, db_name, change["_id"])
            except Exception:
                logger.exception("error realtime-posting eventId=%s", event.get("eventId"))
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
            logger.exception("realtime_posting_worker crashed; restarting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
