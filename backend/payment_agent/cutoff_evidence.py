"""Evidence for the Cutoff Agent: static stage-timing estimates, approval, screening queue, funds and
open exceptions. Direct pymongo reads on the shared DB, like `recon_evidence`.

`on_shift` and `screening_status` mirror transactions `hold_queues` (payment_agent cannot
import it); `test_cutoff_core.py` pins parity by loading that module by file path.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Optional

import cutoff_risk as risk_mod
from cutoff_window import ET, to_et

# One collection, discriminated by `kind`; mirrors transactions `shared/cutoff_workflow.py`.
WORKFLOW = "cutoffWorkflow"
APPROVAL_REQUEST = "APPROVAL_REQUEST"
SCREENING_ITEM = "SCREENING_ITEM"
STAFF_DIRECTORY = "staffDirectory"
OPEN = "OPEN"


# The stages a payment still has to leave after its hold is released. Only stages every
# journey passes through (PENDING_SCREENING is optional).
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
#
# Static estimates, not measured history. They are the minutes a stage takes at p50 and p90
# for a wire; the late-day factors model the thinner evening staffing and the Fedwire rush.
# Summing p90s is deliberately conservative: a journey's true p90 is lower than the sum.

# (p50, p90) minutes a payment spends in each stage it leaves.
_FAST = (0.03, 0.06)                          # automated stages: seconds
STAGE_MINUTES = {
    "VALIDATED": _FAST, "ENRICHED": _FAST, "FINAL_VALIDATED": _FAST, "APPROVED": _FAST,
    "ROUTED": (3.0, 6.5),
    "AUTHORISED": (4.0, 8.6),
    risk_mod.PENDING_APPROVAL: (25.0, 69.8),
    risk_mod.PENDING_SCREENING: (5.0, 9.0),
}
SCREENING_LATE_FROM_ET_MINUTES = 17 * 60      # one analyst carries the queue
SCREENING_LATE_FACTOR = 2.0
EXECUTION_LATE_FROM_ET_MINUTES = 18 * 60      # the Fedwire rush slows routing and authorising
EXECUTION_LATE_FACTOR = 1.5


def _stage_minutes(stage: str, *, at: datetime) -> Optional[tuple]:
    """(p50, p90) minutes for `stage` at business time `at`; None when the stage has no estimate."""
    base = STAGE_MINUTES.get(stage)
    if base is None:
        return None
    now = _tod(at)
    factor = 1.0
    if stage == risk_mod.PENDING_SCREENING and now >= SCREENING_LATE_FROM_ET_MINUTES:
        factor = SCREENING_LATE_FACTOR
    elif stage in ("ROUTED", "AUTHORISED") and now >= EXECUTION_LATE_FROM_ET_MINUTES:
        factor = EXECUTION_LATE_FACTOR
    return round(base[0] * factor, 2), round(base[1] * factor, 2)


def _tod(at: datetime) -> int:
    et = to_et(_utc(at))
    return et.hour * 60 + et.minute


def stage_timing(db: Any, *, rail: str, status: str, at: datetime) -> dict:
    """Remaining-journey and current-stage p50/p90 (minutes) for a payment held at `status`.

    `db` and `rail` are unused: the numbers are static estimates (see above). They stay in the
    signature so callers need no change if measured history ever returns.
    """
    stages = DOWNSTREAM.get(status, [])
    steps = [_stage_minutes(stage, at=at) for stage in stages]
    steps = [s for s in steps if s is not None]
    current = _stage_minutes(status, at=at)
    return {
        "rail": rail, "status": status, "stages": stages,
        "remainingP50Min": round(sum(s[0] for s in steps), 1),
        "remainingP90Min": round(sum(s[1] for s in steps), 1),
        "stageP50Min": current[0] if current else None,
        "stageP90Min": current[1] if current else None,
        "method": "static-estimate",
    }


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
        "openRequests": db[WORKFLOW].count_documents(
            {"kind": APPROVAL_REQUEST, "assignedTo": sid, "status": OPEN}),
        "syntheticOpen": db[WORKFLOW].count_documents(
            {"kind": APPROVAL_REQUEST, "assignedTo": sid, "status": OPEN, "synthetic": True}),
    }


def approval_status(db: Any, *, payment_id: str, at: datetime) -> dict:
    """The OPEN approval request, its primary and backup approver, and their workload."""
    req = db[WORKFLOW].find_one(
        {"kind": APPROVAL_REQUEST, "paymentId": payment_id, "status": OPEN})
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
    queue = db[WORKFLOW]
    item = queue.find_one({"kind": SCREENING_ITEM, "paymentId": payment_id, "status": OPEN})
    if item is None:
        return None
    scope = {"kind": SCREENING_ITEM, "demo.clockRunId": (item.get("demo") or {}).get("clockRunId"), "status": OPEN}
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
