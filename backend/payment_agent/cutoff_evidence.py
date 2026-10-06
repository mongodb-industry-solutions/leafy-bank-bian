"""Read-only evidence for the Cutoff Agent: stage timing, approval, screening queue, funds and
open exceptions. Direct pymongo reads on the shared DB, like `recon_evidence`.

`on_shift` and `screening_status` mirror transactions `hold_queues` (payment_agent cannot
import it); `test_cutoff_core.py` pins parity by loading that module by file path.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import cutoff_risk as risk_mod
from cutoff_window import ET, to_et

logger = logging.getLogger(__name__)

STAGE_EVENTS = "paymentStageEvents"
APPROVAL_REQUESTS = "approvalRequests"
SCREENING_QUEUE = "screeningQueue"
STAFF_DIRECTORY = "staffDirectory"
OPEN = "OPEN"

HISTORY_DAYS = 14
TIME_OF_DAY_WINDOW_MIN = 60
LIVE_LOOKBACK = timedelta(hours=2)
LIVE_RECENT_LIMIT = 20

# The stages a payment still has to leave after its hold is released. Only stages every
# journey passes through, so a journey's sum is comparable (PENDING_SCREENING is optional).
_INITIATION = ["VALIDATED", "ENRICHED", "FINAL_VALIDATED", "ROUTED", "AUTHORISED", "APPROVED"]
DOWNSTREAM = {
    risk_mod.PENDING_APPROVAL: _INITIATION,
    risk_mod.PENDING_FUNDS: _INITIATION,                     # funds re-enters at validation
    risk_mod.PENDING_SCREENING: ["AUTHORISED", "APPROVED"],
    risk_mod.CUTOFF_EXCEPTION: ["ROUTED", "AUTHORISED", "APPROVED"],  # resumes at 4b
}


def _utc(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _float(value) -> float:
    return float(str(value)) if value is not None else 0.0


# --- timing -------------------------------------------------------------------------------

def percentile(values: list, p: float) -> Optional[float]:
    """Nearest-rank percentile; the Python fallback for `$percentile` (approved Q6)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def _tod(at: datetime) -> int:
    et = to_et(_utc(at))
    return et.hour * 60 + et.minute


def _minutes(ms: Optional[float]) -> Optional[float]:
    return None if ms is None else round(ms / 60000, 1)


def _timing_match(rail: str, stages: list, at: datetime) -> dict:
    return {"meta.rail": rail, "meta.stage": {"$in": stages},
            "at": {"$gte": at - timedelta(days=HISTORY_DAYS), "$lte": at}}


def _tod_bounds(at: datetime) -> tuple[int, int]:
    centre = _tod(at)
    return centre - TIME_OF_DAY_WINDOW_MIN, centre + TIME_OF_DAY_WINDOW_MIN


def _summarise(points: list, *, stages: list, stage: str, at: datetime) -> dict:
    """The timing record from already-filtered points (time of day applied by the caller)."""
    journeys: dict = {}
    for p in points:
        if p["meta"]["stage"] in stages:
            j = journeys.setdefault(p["paymentId"], {"ms": 0, "stages": set()})
            j["ms"] += p["durationMs"]
            j["stages"].add(p["meta"]["stage"])
    sums = [j["ms"] for j in journeys.values() if len(j["stages"]) == len(stages)]
    stage_ms = [p["durationMs"] for p in points if p["meta"]["stage"] == stage]
    mix: dict = {}
    for p in points:
        mix[p.get("source") or "UNKNOWN"] = mix.get(p.get("source") or "UNKNOWN", 0) + 1
    live = sorted((p for p in points if p.get("source") == "LIVE"
                   and _utc(p["at"]) >= at - LIVE_LOOKBACK), key=lambda p: p["at"], reverse=True)
    return {
        "remainingP50Min": _minutes(percentile(sums, 0.5)),
        "remainingP90Min": _minutes(percentile(sums, 0.9)),
        "journeySamples": len(sums),
        "stageP50Min": _minutes(percentile(stage_ms, 0.5)),
        "stageP90Min": _minutes(percentile(stage_ms, 0.9)),
        "stageSamples": len(stage_ms),
        "sourceMix": mix,
        "liveRecent": [{"paymentId": p.get("paymentId"), "stage": p["meta"]["stage"],
                        "durationMin": _minutes(p["durationMs"]), "at": p["at"]}
                       for p in live[:LIVE_RECENT_LIMIT]],
    }


def _timing_python(db: Any, *, rail: str, stages: list, stage: str, at: datetime) -> dict:
    lo, hi = _tod_bounds(at)
    points = [p for p in db[STAGE_EVENTS].find(_timing_match(rail, stages + [stage], at))
              if lo <= _tod(p["at"]) <= hi]
    return {**_summarise(points, stages=stages, stage=stage, at=at), "method": "python"}


def _timing_aggregate(db: Any, *, rail: str, stages: list, stage: str, at: datetime) -> dict:
    lo, hi = _tod_bounds(at)
    tz = "America/New_York"
    pct = lambda field: {"$percentile": {"input": field, "p": [0.5, 0.9], "method": "approximate"}}
    pipeline = [
        {"$match": _timing_match(rail, stages + [stage], at)},
        {"$addFields": {"_tod": {"$add": [
            {"$multiply": [{"$hour": {"date": "$at", "timezone": tz}}, 60]},
            {"$minute": {"date": "$at", "timezone": tz}}]}}},
        {"$match": {"_tod": {"$gte": lo, "$lte": hi}}},
        {"$facet": {
            "journeys": [
                {"$match": {"meta.stage": {"$in": stages}}},
                {"$group": {"_id": "$paymentId", "ms": {"$sum": "$durationMs"},
                            "stages": {"$addToSet": "$meta.stage"}}},
                {"$match": {"stages": {"$size": len(stages)}}},
                {"$group": {"_id": None, "n": {"$sum": 1}, "p": pct("$ms")}},
            ],
            "stage": [
                {"$match": {"meta.stage": stage}},
                {"$group": {"_id": None, "n": {"$sum": 1}, "p": pct("$durationMs")}},
            ],
            "mix": [{"$group": {"_id": "$source", "n": {"$sum": 1}}}],
            "live": [
                {"$match": {"source": "LIVE", "at": {"$gte": at - LIVE_LOOKBACK}}},
                {"$sort": {"at": -1}}, {"$limit": LIVE_RECENT_LIMIT},
                {"$project": {"_id": 0, "paymentId": 1, "meta": 1, "durationMs": 1, "at": 1}},
            ],
        }},
    ]
    out = next(iter(db[STAGE_EVENTS].aggregate(pipeline)), {})
    journeys = (out.get("journeys") or [{}])[0]
    stage_row = (out.get("stage") or [{}])[0]
    jp, sp = journeys.get("p") or [None, None], stage_row.get("p") or [None, None]
    return {
        "remainingP50Min": _minutes(jp[0]), "remainingP90Min": _minutes(jp[1]),
        "journeySamples": journeys.get("n", 0),
        "stageP50Min": _minutes(sp[0]), "stageP90Min": _minutes(sp[1]),
        "stageSamples": stage_row.get("n", 0),
        "sourceMix": {r["_id"] or "UNKNOWN": r["n"] for r in out.get("mix") or []},
        "liveRecent": [{"paymentId": p.get("paymentId"), "stage": p["meta"]["stage"],
                        "durationMin": _minutes(p["durationMs"]), "at": p["at"]}
                       for p in out.get("live") or []],
        "method": "$percentile",
    }


def stage_timing(db: Any, *, rail: str, status: str, at: datetime) -> dict:
    """Remaining-journey and current-stage p50/p90 (minutes) for a payment held at `status`.

    Last 14 days, same rail, ET time of day within ±60 min of `at`, up to `at` (seeded points
    for later today are not yet history). `$percentile` needs MongoDB 7.0; on any failure the
    same numbers are computed in Python from the matched points.
    """
    stages = DOWNSTREAM.get(status, [])
    base = {"rail": rail, "status": status, "stages": stages}
    try:
        return {**base, **_timing_aggregate(db, rail=rail, stages=stages, stage=status, at=at)}
    except Exception:  # noqa: BLE001 — fall back; timing is evidence, not a precondition.
        logger.info("stage_timing: $percentile path unavailable; using Python", exc_info=True)
        return {**base, **_timing_python(db, rail=rail, stages=stages, stage=status, at=at)}


# --- people and queues --------------------------------------------------------------------

def _hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def on_shift(staff: dict, at: datetime) -> bool:
    """Mirror of transactions `hold_queues.on_shift`."""
    ooo = staff.get("outOfOfficeUntil")
    if ooo is not None and _utc(at) < _utc(ooo):
        return False
    shift = staff.get("shift") or {}
    if not shift.get("startEt") or not shift.get("endEt"):
        return False
    local = to_et(_utc(at)).time()
    return _hhmm(shift["startEt"]) <= local < _hhmm(shift["endEt"])


def _person(db: Any, staff: Optional[dict], at: datetime) -> Optional[dict]:
    if staff is None:
        return None
    sid = staff.get("staffId")
    return {
        "staffId": sid, "name": staff.get("name"), "onShift": on_shift(staff, at),
        "shift": staff.get("shift"), "outOfOfficeUntil": staff.get("outOfOfficeUntil"),
        "openRequests": db[APPROVAL_REQUESTS].count_documents({"assignedTo": sid, "status": OPEN}),
        "syntheticOpen": db[APPROVAL_REQUESTS].count_documents(
            {"assignedTo": sid, "status": OPEN, "synthetic": True}),
    }


def approval_status(db: Any, *, payment_id: str, at: datetime) -> dict:
    """The OPEN approval request, its primary and backup approver, and their workload."""
    req = db[APPROVAL_REQUESTS].find_one({"paymentId": payment_id, "status": OPEN})
    if req is None:
        return {"open": False}
    approvers = {s.get("staffId"): s for s in db[STAFF_DIRECTORY].find({"role": "APPROVER"})}
    primary_id = req.get("primaryApprover")
    backup = next((s for s in approvers.values()
                   if primary_id and s.get("backupFor") == primary_id), None)
    return {
        "open": True,
        "approvalRequestId": req.get("approvalRequestId"),
        "requestedAt": req.get("requestedAt"),
        "minutesOpen": round((at - _utc(req["requestedAt"])).total_seconds() / 60, 1)
        if req.get("requestedAt") else None,
        "assignedTo": req.get("assignedTo"),
        "reminders": len(req.get("reminders") or []),
        "escalatedTo": req.get("escalatedTo"),
        "primary": _person(db, approvers.get(primary_id), at),
        "backup": _person(db, backup, at),
    }


def screening_status(db: Any, *, payment_id: str, at: datetime) -> Optional[dict]:
    """Mirror of transactions `hold_queues.screening_position`, plus the item and run max."""
    queue = db[SCREENING_QUEUE]
    item = queue.find_one({"paymentId": payment_id, "status": OPEN})
    if item is None:
        return None
    scope = {"demo.clockRunId": (item.get("demo") or {}).get("clockRunId"), "status": OPEN}
    ahead = (
        queue.count_documents({**scope, "priority": {"$gt": item["priority"]}})
        + queue.count_documents({**scope, "priority": item["priority"],
                                 "queuedAt": {"$lt": item["queuedAt"]}})
    )
    on = sum(1 for s in db[STAFF_DIRECTORY].find({"role": "ANALYST"}) if on_shift(s, at))
    run_max = max((d.get("priority") or 0 for d in queue.find(scope)), default=0)
    return {"position": ahead + 1, "ahead": ahead, "analystsOnShift": on,
            "screeningItemId": item.get("screeningItemId"), "priority": item.get("priority"),
            "queuedAt": item.get("queuedAt"), "assignedAnalyst": item.get("assignedAnalyst"),
            "runMaxPriority": run_max}


def _et_instant(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, _hhmm(hhmm), tzinfo=ET).astimezone(timezone.utc)


def funds_position(db: Any, payment: dict, *, business_date: date) -> dict:
    """Available balance, shortfall and expected credits (dated on `business_date`)."""
    account_id = (payment.get("debtor") or {}).get("accountId")
    account = db["accounts"].find_one({"accountId": account_id}) or {}
    available = _float((account.get("balance") or {}).get("available"))
    amount = _float(payment.get("instructedAmount"))
    credits = []
    for c in account.get("expectedCredits") or []:
        expected_at = _et_instant(business_date, c["expectedAtEt"]) if c.get("expectedAtEt") else None
        credits.append({**c, "amount": _float(c.get("amount")), "expectedAt": expected_at})
    return {"accountId": account_id, "available": available, "amount": amount,
            "shortfall": round(max(0.0, amount - available), 2), "expectedCredits": credits}


def open_exceptions(db: Any, payment_id: str) -> dict:
    rows = list(db["exceptions"].find({"paymentId": payment_id, "status": OPEN}))
    return {"count": len(rows),
            "categories": sorted({r.get("category") for r in rows if r.get("category")})}
