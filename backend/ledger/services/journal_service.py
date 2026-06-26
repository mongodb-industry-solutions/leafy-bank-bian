"""Journal service — Stage ③ write logic.

Pure builder + DB writer for the subLedgerEntries → journalEntries summarizer.
The batch worker calls these; keeping the logic here makes it unit-testable
without a scheduler.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
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
) -> tuple[dict, list[str], list[str]]:
    """Assemble ONE balanced summary journal for a (batch window, period).

    agg_rows: list of {_id:{controlAccountCode,side}, amount, currency, count, subLedgerIds, eventIds}.
    Returns (journal_doc, all_subLedgerIds, all_eventIds).
    """
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
        "idempotencyKey": f"JOURNAL-{batch_id}-{period_code}",
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
            "sourceId": batch_id,
            "sourceType": _SOURCE_TYPE_BATCH,
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
