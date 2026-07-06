"""Realtime posting worker — Stage ③ for REALTIME/NEAR_REALTIME events only.

Change stream on `ledgerEvents` updates. Reacts to the `postingResult` update
that `subledger_service.write_subledger_entries` makes right after Stage ②,
and for REALTIME/NEAR_REALTIME events, posts one per-transaction journal
immediately. BATCH events are left untouched for gl_batch's scheduled sweep.

Kept as its own change stream (rather than folded into projection_worker)
so REALTIME payments still show a distinct Stage ②→③ transition in the
pipeline trace. gl_batch.sweep_stale_realtime_postings is the fallback if
this worker's resume token is ever lost — see that function's docstring.

Resume token persisted in `changeStreamTokens`.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo.errors import DuplicateKeyError, OperationFailure

from database.connection import MongoDBConnection
from services.journal_service import REALTIME_POSTING_MODE_TYPES, post_realtime_event
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


def process_ledger_event_update(
    event: dict,
    connection: MongoDBConnection,
    db_name: str,
    coa: ChartOfAccounts,
) -> None:
    event_id = event.get("eventId")
    if not event_id:
        logger.error("ledgerEvent update missing eventId; skipping: %r", event)
        return

    if event.get("postingMode", {}).get("type") not in REALTIME_POSTING_MODE_TYPES:
        return  # BATCH events stay on the gl_batch path

    try:
        post_realtime_event(event_id, connection, db_name, coa=coa)
    except DuplicateKeyError:
        logger.info("realtime journal already exists for eventId=%s; skipping", event_id)
    except Exception:
        # A Stage-③ failure here must not be conflated with a Stage-② failure —
        # the subledger legs already posted successfully. Leave postingStatus
        # PENDING; gl_batch's stale-postings sweep is the fallback rescue.
        logger.exception("realtime journal post failed for eventId=%s; leaving PENDING", event_id)


def run(connection: MongoDBConnection, db_name: str, coa: ChartOfAccounts) -> None:
    logger.info("realtime_posting_worker starting — watching ledgerEvents on %s", db_name)
    ledger_events = connection.get_collection(db_name, "ledgerEvents")
    pipeline = [{"$match": {
        "operationType": "update",
        # The whole-object $set write_subledger_entries makes right after Stage
        # ② — write_journal's own later update sets dotted fields instead
        # (postingStatus, postingResult.journalEntryId, postingResult.postedAt),
        # so it never re-triggers this worker on its own writes.
        "updateDescription.updatedFields.postingResult": {"$exists": True},
    }}]

    # Loops (rather than watching once) because NonResumableChangeStreamError can be
    # raised by a getMore on an already-open cursor, not just by the initial watch()
    # call — a stream that falls behind the oplog window mid-iteration hits the same
    # error later. Both cases need the same clear-token-and-reopen recovery.
    while True:
        resume_token = _load_resume_token(connection, db_name)
        kwargs: dict = {"resume_after": resume_token} if resume_token else {}
        try:
            with ledger_events.watch(pipeline, full_document="updateLookup", **kwargs) as stream:
                for change in stream:
                    event = change.get("fullDocument") or {}
                    try:
                        process_ledger_event_update(event, connection, db_name, coa)
                        _save_resume_token(connection, db_name, change["_id"])
                    except Exception:
                        logger.exception("error processing eventId=%s", event.get("eventId"))
                        raise
        except OperationFailure as exc:
            if exc.has_error_label("NonResumableChangeStreamError") and resume_token is not None:
                logger.critical(
                    "resume token no longer in oplog (%s); clearing and starting a fresh stream — "
                    "any REALTIME ledgerEvents updated since the last saved token were NOT journaled "
                    "by this worker; gl_batch's stale-postings sweep will pick them up.",
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
            logger.exception("realtime_posting_worker crashed; restarting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
