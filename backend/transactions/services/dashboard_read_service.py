"""Dashboard read service: one round trip of aggregates for the Payments Operations page.

Read-only, like `workflow_read_service`. The UI asks for a window (24h / 7d / 30d) and gets
every widget's data already grouped, zero-filled and compared with the previous window, so the
browser only maps and draws. Buckets are hourly for 24h and daily for longer windows.
"""

from __future__ import annotations

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
    return {
        "window": {"name": window, "from": start, "to": end, "bucket": unit},
        "kpis": {
            **kpis,
            "successRate": round(kpis["completed"] / kpis["total"] * 100, 1) if kpis["total"] else None,
            "previous": previous,
        },
        "volume": {"types": rails, "series": fill_series(starts, volume_rows, rails)},
        "stages": stage_breakdown(by_status),
        "types": by_type,
        "exceptionTrend": fill_series(starts, trend_rows, ["count"]),
        "exceptionReasons": reasons,
        "attention": workflow_read_service.list_exceptions(connection, db_name, limit=5, status="OPEN")["items"],
        "recent": workflow_read_service.list_payments(connection, db_name, limit=5)["items"],
    }
