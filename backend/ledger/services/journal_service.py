"""Journal service — Stage ③ write logic.

Pure builder + DB writer for the subLedgerEntries → journalEntries summarizer.
The batch worker calls these; keeping the logic here makes it unit-testable
without a scheduler.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from bson import Int64, ObjectId
from pymongo.errors import DuplicateKeyError

from database.connection import MongoDBConnection
from shared.posting_rules import MAPPING_VERSION
from shared.refs import PREFIX_JOURNAL_ENTRY, derive_ref

if TYPE_CHECKING:
    from shared.coa_cache import ChartOfAccounts

logger = logging.getLogger(__name__)

# Sentinel: subLedgerEntries born without a journal (journalEntryId is required by v30 schema).
_UNJOURNALED = ""

# BIAN SourceSystemReference / SourceTransactionType constants for journalEntries.
_SOURCE_SYSTEM = "GL_BATCH_PIPELINE"
_SOURCE_TYPE_BATCH = "BATCH_POSTING"
_SOURCE_TYPE_REALTIME = "REALTIME_POSTING"

REALTIME_POSTING_MODE_TYPES = ("REALTIME", "NEAR_REALTIME")

# Safety-net threshold for sweep_stale_realtime_postings: how long a REALTIME
# subledger leg pair may sit un-journaled before gl_batch treats it as missed
# by realtime_posting_worker (e.g. a lost resume token) and posts it itself.
_DEFAULT_STALE_REALTIME_SECONDS = 120


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _window_bucket(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M")   # interval label + idempotency component


def assert_balanced_journal(entries: list[dict]) -> None:
    """Guard: ΣDEBIT amount == ΣCREDIT amount across the journal lines."""
    debit = sum(e["amount"] for e in entries if e["side"] == "DEBIT")
    credit = sum(e["amount"] for e in entries if e["side"] == "CREDIT")
    if debit != credit:
        raise ValueError(f"unbalanced journal: sum(DEBIT)={debit} != sum(CREDIT)={credit}")


def build_journal_entry(
    batch_id: str,
    period_code: str,
    agg_rows: list[dict],
    coa: Optional["ChartOfAccounts"] = None,
    *,
    idempotency_key: Optional[str] = None,
    source_type: str = _SOURCE_TYPE_BATCH,
    source_id: Optional[str] = None,
) -> tuple[dict, list[str], list[str]]:
    """Assemble ONE balanced journal for a (window, period).

    Batch path: one line per (controlAccountCode, side) summarizing many txns.
    Realtime path: same shape with count=1 rows — one journal per transaction.

    agg_rows: list of {_id:{controlAccountCode,side}, amount, currency, count, subLedgerIds, eventIds}.
    idempotency_key / source_type / source_id default to the batch values.
    Returns (journal_doc, all_subLedgerIds, all_eventIds).
    """
    idempotency_key = idempotency_key or f"JOURNAL-{batch_id}-{period_code}"
    source_id = source_id or batch_id
    # One line per (controlAccountCode, side). DEBIT-first, then accountCode asc.
    lines_in = sorted(
        agg_rows,
        key=lambda r: (0 if r["_id"]["side"] == "DEBIT" else 1, r["_id"]["controlAccountCode"]),
    )
    entries, sub_ids, event_ids = [], [], []
    label = batch_id.replace("BATCH-", "")  # e.g. 20260623T1415
    for line_num, r in enumerate(lines_in, start=1):
        code = r["_id"]["controlAccountCode"]
        side = r["_id"]["side"]
        if coa is not None:
            acct = coa.require_active_control_account(code)   # exists+ACTIVE, non-posting
        else:
            acct = {}
        amount = Int64(r["amount"])                      # money must be BSON long
        entries.append({
            "lineNumber": line_num,
            "accountCode": code,
            "accountName": acct.get("accountName"),
            "side": side,
            "amount": amount,
            "currency": r.get("currency") or "USD",
            "functionalAmount": amount,
            "lineDescription": f"Sum of {r['count']} {side.lower()} postings to control account {code} — {label}",
        })
        sub_ids.extend(r["subLedgerIds"])
        event_ids.extend(r["eventIds"])

    assert_balanced_journal(entries)   # ΣDR==ΣCR within the document (design §7 / validator)

    oid = ObjectId()
    now = _now_utc()
    total_amount = Int64(sum(int(e["amount"]) for e in entries if e["side"] == "DEBIT"))
    currency = entries[0]["currency"] if entries else "USD"
    journal = {
        "_id": oid,
        "journalId": derive_ref(PREFIX_JOURNAL_ENTRY, oid),
        "idempotencyKey": idempotency_key,
        "periodCode": period_code,
        "valueDate": now.date().isoformat(),                     # string per v30
        "postingDate": now.date().isoformat(),                   # string per v30
        "journalType": "SYSTEM",
        "status": "POSTED",
        "currency": currency,
        "totalAmount": total_amount,
        "entries": entries,
        "createdBy": "GL_BATCH_PIPELINE",                        # required; SoD $ne approvedBy (leave approvedBy absent)
        "sourceReference": {
            "sourceSystem": _SOURCE_SYSTEM,
            "sourceId": source_id,
            "sourceType": source_type,
            "sourceCollection": "subLedgerEntries",
            "periodCode": period_code,
            "txnCount": len(set(event_ids)),
        },
        "mappingVersion": MAPPING_VERSION,
        "createdAt": now.isoformat(),                            # string per v30
        "updatedAt": now.isoformat(),
    }
    return journal, sub_ids, event_ids


def write_journal(
    journal: dict,
    sub_ids: list[str],
    event_ids: list[str],
    connection: MongoDBConnection,
    db_name: str,
) -> bool:
    """Insert the journal, flip subLedger + ledgerEvent statuses, all in one ACID transaction.

    Returns True if written, False if already existed (idempotent replay).
    """
    journal_id = journal["journalId"]
    now = journal["updatedAt"]

    jnl_coll = connection.get_collection(db_name, "journalEntries")
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")
    le_coll = connection.get_collection(db_name, "ledgerEvents")

    try:
        with connection.client.start_session() as session:
            with session.start_transaction():
                jnl_coll.insert_one(journal, session=session)
                sl_coll.update_many(
                    {"subLedgerId": {"$in": sub_ids}},
                    {"$set": {"journalEntryId": journal_id, "updatedAt": now}},
                    session=session,
                )
                le_coll.update_many(
                    {"eventId": {"$in": event_ids}},
                    {"$set": {
                        "postingStatus": "POSTED",
                        "postingResult.journalEntryId": journal_id,
                        "postingResult.postedAt": _now_utc(),
                    }},
                    session=session,
                )
    except DuplicateKeyError:
        logger.info(
            "journal already exists for idempotencyKey=%s; skipping",
            journal["idempotencyKey"],
        )
        return False

    logger.info(
        "journal %s posted (period=%s, lines=%d)",
        journal_id, journal["periodCode"], len(journal["entries"]),
    )
    return True


def post_realtime_event(
    event_id: str,
    connection: MongoDBConnection,
    db_name: str,
    coa: Optional["ChartOfAccounts"] = None,
) -> bool:
    """Stage ③ (realtime): write ONE per-transaction journal for a single event.

    Reads the event's two POSTED, un-journaled subLedgerEntries and summarizes
    them into one balanced journal (one line per control account + side).
    Unlike run_batch, this produces a journal per transaction, not per window.
    Idempotent via journalEntries.idempotencyKey. Returns True if written.
    """
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")
    rows = list(sl_coll.find({
        "sourceReference.sourceId": event_id,
        "status": "POSTED",
        "journalEntryId": _UNJOURNALED,
    }))
    if not rows:
        logger.info("no un-journaled subLedgerEntries for eventId=%s; realtime post skipped", event_id)
        return False

    # Must be a complete DEBIT+CREDIT pair in a single period, mirroring the
    # batch completeness gate (GAP-2). A partial pair must never be journaled.
    sides = {r["side"] for r in rows}
    periods = {r["periodCode"] for r in rows}
    if len(rows) != 2 or sides != {"DEBIT", "CREDIT"}:
        logger.warning(
            "incomplete leg pair for eventId=%s (count=%d sides=%s) — realtime post skipped",
            event_id, len(rows), sorted(sides),
        )
        return False
    if len(periods) > 1:
        logger.warning(
            "period mismatch for eventId=%s (periods=%s) — realtime post skipped",
            event_id, sorted(periods),
        )
        return False
    period_code = periods.pop()

    # Reshape the two legs into the aggregation shape build_journal_entry expects.
    agg_rows = [
        {
            "_id": {"controlAccountCode": r["controlAccountCode"], "side": r["side"]},
            "amount": r["amount"],
            "currency": r.get("currency"),
            "count": 1,
            "subLedgerIds": [r["subLedgerId"]],
            "eventIds": [event_id],
        }
        for r in rows
    ]

    journal, sub_ids, event_ids = build_journal_entry(
        f"RT-{event_id}",
        period_code,
        agg_rows,
        coa=coa,
        idempotency_key=f"JOURNAL-RT-{event_id}",
        source_type=_SOURCE_TYPE_REALTIME,
        source_id=event_id,
    )
    return write_journal(journal, sub_ids, event_ids, connection, db_name)


def run_batch(
    connection: MongoDBConnection,
    db_name: str,
    coa: Optional["ChartOfAccounts"] = None,
) -> int:
    """Sweep all un-journaled POSTED subledger rows, write one summary journal per period.

    Returns the count of journals written this run.
    """
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")
    now = _now_utc()
    batch_id = f"BATCH-{_window_bucket(now)}"

    # GAP-2: verify each event contributes exactly one DEBIT + one CREDIT before aggregating.
    # A partial pair (one leg FAILED or missing) must never enter a journal.
    completeness_rows = list(sl_coll.aggregate([
        {"$match": {"status": "POSTED", "journalEntryId": _UNJOURNALED}},
        {"$group": {
            "_id": "$sourceReference.sourceId",
            "count": {"$sum": 1},
            "sides": {"$addToSet": "$side"},
            "periodCodes": {"$addToSet": "$periodCode"},
        }},
    ]))
    complete_event_ids: list[str] = []
    for r in completeness_rows:
        sides = set(r["sides"])
        periods = set(r["periodCodes"])
        if r["count"] != 2 or sides != {"DEBIT", "CREDIT"}:
            logger.warning(
                "incomplete leg pair for eventId=%s (count=%d sides=%s) — excluded from batch",
                r["_id"], r["count"], sorted(r["sides"]),
            )
        elif len(periods) > 1:
            logger.warning(
                "period mismatch for eventId=%s (periods=%s) — excluded from batch",
                r["_id"], sorted(periods),
            )
        else:
            complete_event_ids.append(r["_id"])

    if not complete_event_ids:
        return 0

    # postingMode routing: REALTIME / NEAR_REALTIME events are journaled
    # per-transaction inline by projection_worker (right after it writes the
    # subledger legs). The batch posts ONLY postingMode=BATCH events, so the
    # two paths never journal the same event.
    le_coll = connection.get_collection(db_name, "ledgerEvents")
    complete_event_ids = [
        d["eventId"]
        for d in le_coll.find(
            {"eventId": {"$in": complete_event_ids}, "postingMode.type": "BATCH"},
            {"eventId": 1},
        )
    ]
    if not complete_event_ids:
        return 0

    # Group by (period, control account, side) → one summarized line each.
    # Only legs from complete event pairs are included.
    pipeline = [
        {"$match": {
            "status": "POSTED",
            "journalEntryId": _UNJOURNALED,
            "sourceReference.sourceId": {"$in": complete_event_ids},
        }},
        {"$group": {
            "_id": {
                "periodCode": "$periodCode",
                "controlAccountCode": "$controlAccountCode",
                "side": "$side",
            },
            "amount": {"$sum": "$amount"},
            "currency": {"$first": "$currency"},
            "count": {"$sum": 1},
            "subLedgerIds": {"$push": "$subLedgerId"},
            "eventIds": {"$addToSet": "$sourceReference.sourceId"},
        }},
    ]
    rows = list(sl_coll.aggregate(pipeline))

    # Partition by period → one balanced journal document per period.
    by_period: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_period[r["_id"]["periodCode"]].append(r)

    written = 0
    for period_code, agg_rows in by_period.items():
        try:
            journal, sub_ids, event_ids = build_journal_entry(
                batch_id, period_code, agg_rows, coa=coa
            )
            if write_journal(journal, sub_ids, event_ids, connection, db_name):
                written += 1
        except ValueError as e:
            logger.error("skipping batch=%s period=%s: %s", batch_id, period_code, e)
        except Exception:
            logger.exception("unexpected error batch=%s period=%s", batch_id, period_code)
    return written


def sweep_stale_realtime_postings(
    connection: MongoDBConnection,
    db_name: str,
    coa: Optional["ChartOfAccounts"] = None,
    stale_after_seconds: int = _DEFAULT_STALE_REALTIME_SECONDS,
) -> int:
    """Safety net for REALTIME events realtime_posting_worker failed to journal.

    gl_batch never aggregates REALTIME events (see the postingMode routing filter
    in run_batch above), so if realtime_posting_worker's change stream ever loses
    its resume token, an un-journaled REALTIME leg pair would otherwise sit in
    subLedgerEntries forever with no worker responsible for it. This sweep finds
    any such leg pairs older than `stale_after_seconds` and posts them itself via
    post_realtime_event — the same per-transaction journal the dedicated worker
    would have written. Returns the count of journals posted.
    """
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")
    le_coll = connection.get_collection(db_name, "ledgerEvents")

    cutoff = (_now_utc() - timedelta(seconds=stale_after_seconds)).isoformat()
    stale_event_ids = sl_coll.distinct(
        "sourceReference.sourceId",
        {"status": "POSTED", "journalEntryId": _UNJOURNALED, "postingDate": {"$lt": cutoff}},
    )
    if not stale_event_ids:
        return 0

    # A stale-looking BATCH event is normal (it's waiting for the next gl_batch
    # window) — only REALTIME/NEAR_REALTIME events are this sweep's concern.
    realtime_event_ids = [
        d["eventId"]
        for d in le_coll.find(
            {"eventId": {"$in": stale_event_ids}, "postingMode.type": {"$in": list(REALTIME_POSTING_MODE_TYPES)}},
            {"eventId": 1},
        )
    ]

    posted = 0
    for event_id in realtime_event_ids:
        try:
            if post_realtime_event(event_id, connection, db_name, coa=coa):
                posted += 1
                logger.warning(
                    "gl_batch safety net posted stale REALTIME event %s "
                    "(realtime_posting_worker did not journal it in time)",
                    event_id,
                )
        except Exception:
            logger.exception("safety-net realtime post failed for eventId=%s", event_id)
    return posted
