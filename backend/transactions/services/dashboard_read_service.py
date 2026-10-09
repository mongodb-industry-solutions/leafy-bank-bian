"""Dashboard read service: one round trip of aggregates for the Payments Operations page.

Read-only, like `workflow_read_service`. The UI asks for a window (24h / 7d / 30d) and gets
every widget's data already grouped, zero-filled and compared with the previous window, so the
browser only maps and draws. Buckets are hourly for 24h and daily for longer windows.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from contexts.payment_order_initiation.domain import lifecycle
from database.connection import MongoDBConnection
from services import workflow_read_service

# window -> (length, bucket unit, bucket size)
WINDOWS = {
    "24h": (timedelta(hours=24), "hour", timedelta(hours=1)),
    "7d": (timedelta(days=7), "day", timedelta(days=1)),
    "30d": (timedelta(days=30), "day", timedelta(days=1)),
}

# Dwell averages do not need every payment: the newest N is a representative sample, and the
# cap keeps the `$unwind` over `lifecycle.events[]` bounded as the collection grows.
DWELL_SAMPLE_LIMIT = 5000

# Every open dashboard tab polls; one computation per window per TTL serves them all.
CACHE_TTL_SECONDS = 15

COMPLETED_STATES = (lifecycle.SETTLED, lifecycle.RECONCILED)
EXCEPTION_STATES = tuple(sorted(lifecycle.TERMINALS))

# Display stages, in lifecycle order. Every state lands in exactly one; anything unlisted
# falls into "Other" so the donut always sums to the total.
STAGE_BUCKETS = [
    ("Initiated", (lifecycle.DRAFT, lifecycle.INITIATED, lifecycle.RECEIVED)),
    ("Validated", (lifecycle.VALIDATED, lifecycle.FINAL_VALIDATED)),
    ("Enriched", (lifecycle.ENRICHED,)),
    ("Authorised", (lifecycle.ROUTED, lifecycle.AUTHORISED, lifecycle.APPROVED,
                    lifecycle.ACCEPTED, lifecycle.SUBMITTED)),
    ("On hold", (lifecycle.MANUAL_FRAUD_REVIEW, lifecycle.PENDING_APPROVAL, lifecycle.PENDING_FUNDS,
                 lifecycle.CUTOFF_EXCEPTION, lifecycle.PENDING_SCREENING)),
    ("In progress", (lifecycle.IN_PROGRESS,)),
    ("Posted", (lifecycle.POSTED,)),
    ("Settled", (lifecycle.SETTLED,)),
    ("Reconciled", (lifecycle.RECONCILED,)),
    ("Exception", EXCEPTION_STATES),
]


def _floor(moment: datetime, unit: str) -> datetime:
    if unit == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def bucket_starts(end: datetime, length: timedelta, unit: str, step: timedelta) -> list[datetime]:
    """Aligned bucket start times covering (end - length, end], oldest first."""
    last = _floor(end, unit)
    count = int(length / step)
    return [last - step * i for i in range(count - 1, -1, -1)]


def fill_series(starts: list[datetime], rows: list[dict], keys: list[str]) -> list[dict]:
    """Zero-filled series. `rows` are `{_id: {t, k}, n}`; one output point per bucket."""
    by_bucket: dict[datetime, dict[str, int]] = {}
    for row in rows:
        t = row["_id"]["t"]
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        by_bucket.setdefault(t, {})[row["_id"].get("k")] = int(row["n"])
    points = []
    for start in starts:
        counts = by_bucket.get(start, {})
        point = {"t": start, **{k: counts.get(k, 0) for k in keys}}
        point["total"] = sum(point[k] for k in keys)
        points.append(point)
    return points


def stage_breakdown(by_status: dict[str, int]) -> list[dict]:
    placed: set[str] = set()
    out = []
    for label, states in STAGE_BUCKETS:
        placed.update(states)
        out.append({"stage": label, "count": sum(by_status.get(s, 0) for s in states)})
    other = sum(n for s, n in by_status.items() if s not in placed)
    if other:
        out.append({"stage": "Other", "count": other})
    return out


def _kpis(by_status: dict[str, int]) -> dict:
    total = sum(by_status.values())
    completed = sum(by_status.get(s, 0) for s in COMPLETED_STATES)
    exceptions = sum(by_status.get(s, 0) for s in EXCEPTION_STATES)
    return {
        "total": total,
        "completed": completed,
        "inProgress": max(total - completed - exceptions, 0),
        "exceptions": exceptions,
    }


def _status_histogram(coll, start: datetime, end: datetime) -> dict[str, int]:
    pipeline = [
        {"$match": {"createdAt": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
    ]
    return {r["_id"]: int(r["n"]) for r in coll.aggregate(pipeline) if r["_id"]}


def _bucketed(coll, match: dict, field: str, key_expr, unit: str) -> list[dict]:
    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": {"t": {"$dateTrunc": {"date": f"${field}", "unit": unit}}, "k": key_expr},
            "n": {"$sum": 1},
        }},
    ]
    return list(coll.aggregate(pipeline))


def processing_times(rows: list[dict]) -> list[dict]:
    """Average dwell per display stage from per-state `{_id: state, avgMs, n}` rows.

    Weighted by sample count so a rare state does not skew its stage. Stages with no
    samples are omitted rather than shown as zero.
    """
    by_state = {r["_id"]: r for r in rows if r.get("avgMs") is not None}
    out = []
    for label, states in STAGE_BUCKETS:
        if label == "Exception":
            continue
        samples = sum(int(by_state[s]["n"]) for s in states if s in by_state)
        if not samples:
            continue
        total_ms = sum(by_state[s]["avgMs"] * int(by_state[s]["n"]) for s in states if s in by_state)
        out.append({"stage": label, "avgSeconds": round(total_ms / samples / 1000, 1), "samples": samples})
    return out


def _dwell_rows(coll, match: dict) -> list[dict]:
    """Time spent in each state: gap between consecutive `lifecycle.events[].at`."""
    events = {"$ifNull": ["$lifecycle.events", []]}
    pipeline = [
        {"$match": match},
        {"$sort": {"createdAt": -1}},
        {"$limit": DWELL_SAMPLE_LIMIT},
        {"$project": {"pairs": {"$map": {
            "input": {"$range": [0, {"$max": [0, {"$subtract": [{"$size": events}, 1]}]}]},
            "as": "i",
            "in": {
                "state": {"$arrayElemAt": ["$lifecycle.events.state", "$$i"]},
                "ms": {"$subtract": [
                    {"$arrayElemAt": ["$lifecycle.events.at", {"$add": ["$$i", 1]}]},
                    {"$arrayElemAt": ["$lifecycle.events.at", "$$i"]},
                ]},
            },
        }}}},
        {"$unwind": "$pairs"},
        {"$match": {"pairs.ms": {"$gte": 0}}},
        {"$group": {"_id": "$pairs.state", "avgMs": {"$avg": "$pairs.ms"}, "n": {"$sum": 1}}},
    ]
    return list(coll.aggregate(pipeline))


def _reconciliation(coll, match: dict) -> dict:
    """Reconciliation outcome per payment, from `lifecycle.reconciliationStatus` (written
    onto `payments` by the ledger). Payments not yet at that stage carry no value."""
    counts = {"reconciled": 0, "pending": 0, "discrepancies": 0}
    key = {"RECONCILED": "reconciled", "PENDING": "pending", "DISCREPANT": "discrepancies"}
    for r in coll.aggregate([
        {"$match": {**match, "lifecycle.reconciliationStatus": {"$ne": None}}},
        {"$group": {"_id": "$lifecycle.reconciliationStatus", "n": {"$sum": 1}}},
    ]):
        if r["_id"] in key:
            counts[key[r["_id"]]] = int(r["n"])
    total = sum(counts.values())
    return {**counts, "total": total,
            "reconciledPct": round(counts["reconciled"] / total * 100, 1) if total else None}


def _settlement(connection, db_name: str, match: dict) -> list[dict]:
    """Settlement positions per settlement model (correspondent, central bank, vostro)."""
    coll = connection.get_collection(db_name, "settlementPositions")
    rows = coll.aggregate([
        {"$match": match},
        {"$group": {
            "_id": {"$ifNull": ["$modelLabel", "$model"]},
            "positions": {"$sum": 1},
            "settled": {"$sum": {"$cond": [{"$eq": ["$settlementStatus", "SETTLED"]}, 1, 0]}},
            "delayed": {"$sum": {"$cond": [{"$eq": ["$outcome", "DELAYED"]}, 1, 0]}},
            "returned": {"$sum": {"$cond": [{"$eq": ["$settlementStatus", "RETURNED"]}, 1, 0]}},
        }},
        {"$sort": {"positions": -1}},
    ])
    return [
        {"scheme": r["_id"] or "Unknown", "positions": int(r["positions"]), "settled": int(r["settled"]),
         "delayed": int(r["delayed"]), "returned": int(r["returned"])}
        for r in rows
    ]


def _agent_impact(exceptions, match: dict) -> dict:
    """Reconciliation-agent involvement, from the `agent{}` block on shared `exceptions`.

    Only the agent's own verification counts as autonomous: an operator or the ledger
    watchdog resolving an exception is not credited to the agent.
    """
    agent_match = {**match, "agent": {"$ne": None}}
    involved = resolved = 0
    for r in exceptions.aggregate([
        {"$match": agent_match},
        {"$group": {
            "_id": {"$eq": ["$agent.verification.result", "RESOLVED"]},
            "n": {"$sum": 1},
        }},
    ]):
        involved += int(r["n"])
        if r["_id"] is True:
            resolved += int(r["n"])
    recent = []
    cursor = exceptions.find(agent_match, {
        "_id": 0, "exceptionId": 1, "paymentId": 1, "category": 1, "status": 1, "updatedAt": 1,
        "agent.rootCause": 1, "agent.confidence": 1, "agent.verification": 1,
    }).sort("updatedAt", -1).limit(3)
    for e in cursor:
        agent = e.get("agent") or {}
        recent.append({
            "exceptionId": e.get("exceptionId"), "paymentId": e.get("paymentId"),
            "category": e.get("category"), "status": e.get("status"),
            "rootCause": agent.get("rootCause"), "confidence": agent.get("confidence"),
            "verification": (agent.get("verification") or {}).get("result"),
            "updatedAt": e.get("updatedAt"),
        })
    return {
        "involved": involved, "resolved": resolved,
        "successRate": round(resolved / involved * 100, 1) if involved else None,
        "recent": recent,
    }


def get_dashboard(
    connection: MongoDBConnection,
    db_name: str,
    *,
    window: str = "24h",
    now: Optional[datetime] = None,
) -> dict:
    length, unit, step = WINDOWS[window]
    end = now or datetime.now(timezone.utc)
    start = end - length
    prev_start = start - length

    payments = connection.get_collection(db_name, "payments")
    exceptions = connection.get_collection(db_name, "exceptions")
    starts = bucket_starts(end, length, unit, step)

    by_status = _status_histogram(payments, start, end)
    previous = _kpis(_status_histogram(payments, prev_start, start))

    in_window = {"createdAt": {"$gte": start, "$lt": end}}
    rails = sorted({k for r in payments.aggregate([
        {"$match": in_window}, {"$group": {"_id": "$rail", "n": {"$sum": 1}}},
    ]) if (k := r["_id"])})
    volume_rows = _bucketed(payments, in_window, "createdAt", "$rail", unit)
    by_type = [
        {"type": rail, "count": sum(int(r["n"]) for r in volume_rows if r["_id"].get("k") == rail)}
        for rail in rails
    ]

    exc_match = {"historical": None, "createdAt": {"$gte": start, "$lt": end}}
    trend_rows = _bucketed(exceptions, exc_match, "createdAt", {"$literal": "count"}, unit)
    reasons = [
        {"reason": r["_id"], "count": int(r["n"])}
        for r in exceptions.aggregate([
            {"$match": exc_match},
            {"$group": {"_id": "$category", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
            {"$limit": 6},
        ]) if r["_id"]
    ]

    kpis = _kpis(by_status)
    agent = _agent_impact(exceptions, {"historical": None, "createdAt": {"$gte": start, "$lt": end}})
    return {
        "window": {"name": window, "from": start, "to": end, "bucket": unit},
        "kpis": {
            **kpis,
            "successRate": round(kpis["completed"] / kpis["total"] * 100, 1) if kpis["total"] else None,
            "previous": previous,
            "resolvedByAgents": agent["resolved"],
        },
        "volume": {"types": rails, "series": fill_series(starts, volume_rows, rails)},
        "stages": stage_breakdown(by_status),
        "types": by_type,
        "exceptionTrend": fill_series(starts, trend_rows, ["count"]),
        "exceptionReasons": reasons,
        "processingTimes": processing_times(_dwell_rows(payments, in_window)),
        "reconciliation": _reconciliation(payments, in_window),
        "settlement": _settlement(connection, db_name, in_window),
        "agentImpact": agent,
        "attention": workflow_read_service.list_exceptions(connection, db_name, limit=5, status="OPEN")["items"],
        "recent": workflow_read_service.list_payments(connection, db_name, limit=5)["items"],
    }


_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def get_dashboard_cached(connection: MongoDBConnection, db_name: str, *, window: str = "24h") -> dict:
    """`get_dashboard` behind a short per-window cache. The lock makes concurrent requests
    wait for one computation instead of each running the aggregates."""
    key = (db_name, window)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        data = get_dashboard(connection, db_name, window=window)
        _cache[key] = (time.monotonic(), data)
        return data
