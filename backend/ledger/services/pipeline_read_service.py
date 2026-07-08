"""Pipeline read service — UI-only queries for the GL Pipeline Monitor dashboard.

All functions are read-only. This module is intentionally separate from
gl_read_service (BIAN-facing) — these queries exist purely to power the
monitoring UI and are not part of the BIAN FinancialAccounting contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from database.connection import MongoDBConnection
from services import reconciliation_service


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _current_period() -> str:
    return _now_utc().strftime("%Y-%m")


def _next_month_start(period_code: str) -> datetime:
    year, month = int(period_code[:4]), int(period_code[5:7])
    if month == 12:
        return datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(year, month + 1, 1, tzinfo=timezone.utc)


def _month_start(period_code: str) -> datetime:
    year, month = int(period_code[:4]), int(period_code[5:7])
    return datetime(year, month, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def get_pipeline_health(
    connection: MongoDBConnection,
    db_name: str,
    *,
    batch_interval_seconds: int,
) -> dict:
    """Return counts and timing for the pipeline health bar."""
    le_coll = connection.get_collection(db_name, "ledgerEvents")
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")
    jnl_coll = connection.get_collection(db_name, "journalEntries")

    # One aggregation for all ledgerEvent status counts.
    status_counts: dict[str, int] = {"PENDING": 0, "POSTED": 0, "FAILED": 0}
    for row in le_coll.aggregate([{"$group": {"_id": "$postingStatus", "n": {"$sum": 1}}}]):
        key = row["_id"]
        if key in status_counts:
            status_counts[key] = int(row["n"])

    period = _current_period()
    sl_unjournaled = sl_coll.count_documents({"journalEntryId": ""})
    sl_journaled = sl_coll.count_documents({"journalEntryId": {"$ne": ""}})
    jnl_total = jnl_coll.count_documents({})
    jnl_this_period = jnl_coll.count_documents({"periodCode": period})

    last_journal = jnl_coll.find_one({}, sort=[("createdAt", -1)], projection={"createdAt": 1})
    last_batch_at = None
    if last_journal and "createdAt" in last_journal:
        val = last_journal["createdAt"]
        last_batch_at = val.isoformat() if isinstance(val, datetime) else str(val)

    return {
        "dbName": db_name,
        "ledgerEvents": {
            "pending": status_counts["PENDING"],
            "posted": status_counts["POSTED"],
            "failed": status_counts["FAILED"],
        },
        "subLedger": {
            "unjournaled": sl_unjournaled,
            "journaled": sl_journaled,
        },
        "journals": {
            "total": jnl_total,
            "thisPeriod": jnl_this_period,
        },
        "batchIntervalSeconds": batch_interval_seconds,
        "lastBatchAt": last_batch_at,
    }


# ---------------------------------------------------------------------------
# Collection feeds
# ---------------------------------------------------------------------------

def list_transactions(
    connection: MongoDBConnection,
    db_name: str,
    *,
    limit: int = 20,
    period_code: Optional[str] = None,
) -> dict:
    """Return recent settled transactions from the upstream collection."""
    coll = connection.get_collection(db_name, "transactions")
    le_coll = connection.get_collection(db_name, "ledgerEvents")

    match: dict = {}
    if period_code:
        match["createdAt"] = {
            "$gte": _month_start(period_code),
            "$lt": _next_month_start(period_code),
        }

    total = coll.count_documents(match)
    items = list(
        coll.find(
            match,
            {
                "_id": 0,
                "paymentId": 1,
                "amount": 1,
                "currency": 1,
                "rail": 1,
                "paymentType": 1,
                "transactionStatus": 1,
                "createdAt": 1,
                "payer": 1,
                "payee": 1,
            },
        )
        .sort("createdAt", -1)
        .limit(limit)
    )

    # Attach ledgerEventId (idempotencyKey == paymentId, same join trace_payment uses).
    payment_ids = [item["paymentId"] for item in items]
    ledger_events = le_coll.find(
        {"idempotencyKey": {"$in": payment_ids}},
        {"_id": 0, "idempotencyKey": 1, "eventId": 1},
    )
    event_id_by_payment = {le["idempotencyKey"]: le["eventId"] for le in ledger_events}
    for item in items:
        item["ledgerEventId"] = event_id_by_payment.get(item["paymentId"])

    return {"items": items, "total": total}


def list_ledger_events(
    connection: MongoDBConnection,
    db_name: str,
    *,
    limit: int = 20,
    status: Optional[str] = None,
    period_code: Optional[str] = None,
) -> dict:
    """Return recent ledgerEvents, newest first.

    periodCode lives at meta.periodCode on ledgerEvents — not top-level.
    """
    coll = connection.get_collection(db_name, "ledgerEvents")

    match: dict = {}
    if status:
        match["postingStatus"] = status
    if period_code:
        match["meta.periodCode"] = period_code

    total = coll.count_documents(match)
    items = list(
        coll.find(match, {"_id": 0})
        .sort("occurredAt", -1)
        .limit(limit)
    )
    return {"items": items, "total": total}


def list_subledger_entries(
    connection: MongoDBConnection,
    db_name: str,
    *,
    limit: int = 30,
    period_code: Optional[str] = None,
    control_account_code: Optional[str] = None,
) -> dict:
    """Return recent subLedgerEntries, most recent posting date first.

    periodCode is top-level on subLedgerEntries (set by subledger_service).
    Sort on postingDate DESC is correct — ISO strings with consistent +00:00 offset
    sort lexicographically.
    """
    coll = connection.get_collection(db_name, "subLedgerEntries")

    match: dict = {}
    if period_code:
        match["periodCode"] = period_code
    if control_account_code:
        match["controlAccountCode"] = control_account_code

    total = coll.count_documents(match)
    items = list(
        coll.find(match, {"_id": 0})
        .sort("postingDate", -1)
        .limit(limit)
    )
    return {"items": items, "total": total}


def list_journal_entries(
    connection: MongoDBConnection,
    db_name: str,
    *,
    limit: int = 20,
    period_code: Optional[str] = None,
) -> dict:
    """Return recent journalEntries, newest posting date first."""
    coll = connection.get_collection(db_name, "journalEntries")

    match: dict = {}
    if period_code:
        match["periodCode"] = period_code

    total = coll.count_documents(match)
    items = list(
        coll.find(match, {"_id": 0})
        .sort("postingDate", -1)
        .limit(limit)
    )
    return {"items": items, "total": total}


# ---------------------------------------------------------------------------
# GL dashboard (monthly)
# ---------------------------------------------------------------------------

def get_gl_dashboard(
    connection: MongoDBConnection,
    db_name: str,
    *,
    period_code: Optional[str] = None,
    top_n: int = 5,
) -> dict:
    """Aggregate every GL dashboard block for one month in a single response.

    All amounts are minor units (int), consistent with the rest of this service.
    Scoping is monthly via periodCode ("YYYY-MM"); defaults to the current month.
    """
    period = period_code or _current_period()

    jnl_coll = connection.get_collection(db_name, "journalEntries")
    le_coll = connection.get_collection(db_name, "ledgerEvents")
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")

    # --- KPI tiles: journal count, debit/credit totals, out-of-balance count ---
    # Per-journal DR/CR sums, then roll up. outOfBalance is structurally 0
    # (assert_balanced_journal blocks unbalanced writes) but surfaced for the tile.
    summary_pipeline = [
        {"$match": {"periodCode": period}},
        {"$project": {
            "currency": 1,
            "debit": {"$sum": {"$map": {
                "input": "$entries", "as": "e",
                "in": {"$cond": [{"$eq": ["$$e.side", "DEBIT"]}, "$$e.amount", 0]},
            }}},
            "credit": {"$sum": {"$map": {
                "input": "$entries", "as": "e",
                "in": {"$cond": [{"$eq": ["$$e.side", "CREDIT"]}, "$$e.amount", 0]},
            }}},
        }},
        {"$group": {
            "_id": None,
            "totalJournals": {"$sum": 1},
            "totalDebit": {"$sum": "$debit"},
            "totalCredit": {"$sum": "$credit"},
            "outOfBalance": {"$sum": {"$cond": [{"$ne": ["$debit", "$credit"]}, 1, 0]}},
            "currency": {"$first": "$currency"},
        }},
    ]
    summary_rows = list(jnl_coll.aggregate(summary_pipeline))
    if summary_rows:
        row = summary_rows[0]
        summary = {
            "totalJournals": int(row["totalJournals"]),
            "totalDebit": int(row["totalDebit"]),
            "totalCredit": int(row["totalCredit"]),
            "outOfBalance": int(row["outOfBalance"]),
            "currency": row.get("currency") or "USD",
        }
    else:
        summary = {
            "totalJournals": 0, "totalDebit": 0, "totalCredit": 0,
            "outOfBalance": 0, "currency": "USD",
        }

    # --- Journal Status donut: driven off ledgerEvents.postingStatus for the period.
    # PENDING is surfaced as "posting". Total is the event count (may differ from
    # totalJournals, since one batch journal aggregates many events).
    status_counts: dict[str, int] = {"PENDING": 0, "POSTED": 0, "FAILED": 0}
    status_pipeline = [
        {"$match": {"meta.periodCode": period}},
        {"$group": {"_id": "$postingStatus", "n": {"$sum": 1}}},
    ]
    for r in le_coll.aggregate(status_pipeline):
        if r["_id"] in status_counts:
            status_counts[r["_id"]] = int(r["n"])
    journal_status = {
        "posted": status_counts["POSTED"],
        "posting": status_counts["PENDING"],
        "failed": status_counts["FAILED"],
        "total": sum(status_counts.values()),
    }

    # --- Reconciliation roll-up: reduce per-account checks to one flag.
    recon_results = reconciliation_service.reconcile_all_accounts(
        connection, db_name, period_code=period
    )
    breaks = sum(1 for r in recon_results if not r.is_reconciled)
    reconciliation = {
        "status": "BALANCED" if breaks == 0 else "OUT_OF_BALANCE",
        "accountsChecked": len(recon_results),
        "breaks": breaks,
    }

    # --- Top control accounts: DR/CR split + balance per control account, ranked.
    top_pipeline = [
        {"$match": {"periodCode": period, "status": "POSTED"}},
        {"$group": {
            "_id": "$controlAccountCode",
            "debit": {"$sum": {"$cond": [{"$eq": ["$side", "DEBIT"]}, "$amount", 0]}},
            "credit": {"$sum": {"$cond": [{"$eq": ["$side", "CREDIT"]}, "$amount", 0]}},
        }},
        {"$addFields": {
            "balance": {"$subtract": ["$debit", "$credit"]},
            "activity": {"$add": ["$debit", "$credit"]},
        }},
        {"$sort": {"activity": -1, "_id": 1}},
        {"$limit": top_n},
        {"$lookup": {
            "from": "glAccounts",
            "localField": "_id",
            "foreignField": "accountCode",
            "as": "acct",
        }},
        {"$project": {
            "_id": 0,
            "accountCode": "$_id",
            "accountName": {"$arrayElemAt": ["$acct.accountName", 0]},
            "debit": 1,
            "credit": 1,
            "balance": 1,
        }},
    ]
    top_accounts = [
        {
            "accountCode": r["accountCode"],
            "accountName": r.get("accountName"),
            "debit": int(r["debit"]),
            "credit": int(r["credit"]),
            "balance": int(r["balance"]),
        }
        for r in sl_coll.aggregate(top_pipeline)
    ]

    return {
        "periodCode": period,
        "summary": summary,
        "journalStatus": journal_status,
        "reconciliation": reconciliation,
        "topControlAccounts": top_accounts,
    }


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------

def trace_payment(
    payment_id: str,
    connection: MongoDBConnection,
    db_name: str,
) -> Optional[dict]:
    """Trace one payment through all four GL pipeline stages.

    Returns None if paymentId is not found in transactions (caller raises 404).
    Partial results (stages not yet reached) are present as None in the dict.
    """
    payment_coll = connection.get_collection(db_name, "payments")
    txn_coll = connection.get_collection(db_name, "transactions")
    le_coll = connection.get_collection(db_name, "ledgerEvents")
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")
    jnl_coll = connection.get_collection(db_name, "journalEntries")

    # Stage 1 — transaction (source of truth).
    txn = txn_coll.find_one({"paymentId": payment_id}, {"_id": 0})
    if txn is None:
        return None

    # Payment — payments collection doc (initiation/clearing state, owned by
    # the transactions service). Not a pipeline stage; the initiation record.
    payment = payment_coll.find_one({"paymentId": payment_id}, {"_id": 0})

    # Stage 2 — ledgerEvent (idempotencyKey == paymentId, set by ingest_worker).
    ledger_event = le_coll.find_one({"idempotencyKey": payment_id}, {"_id": 0})

    # Stage 3 — subLedgerEntries (sourceReference.sourceId == eventId).
    subledger_entries = None
    if ledger_event:
        event_id = ledger_event.get("eventId")
        rows = list(sl_coll.find({"sourceReference.sourceId": event_id}, {"_id": 0}))
        subledger_entries = rows if rows else None

    # Stage 4 — journalEntry (journalId stamped on subledger entry after gl_batch).
    journal_entry = None
    if subledger_entries:
        journal_id = subledger_entries[0].get("journalEntryId", "")
        if journal_id:
            journal_entry = jnl_coll.find_one({"journalId": journal_id}, {"_id": 0})

    posting_mode = (ledger_event or {}).get("postingMode", {}).get("type")

    return {
        "paymentId": payment_id,
        "payment": payment,
        "transaction": txn,
        "ledgerEvent": ledger_event,
        "subLedgerEntries": subledger_entries,
        "journalEntry": journal_entry,
        "postingMode": posting_mode,
    }
