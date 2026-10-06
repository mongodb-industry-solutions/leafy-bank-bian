"""The `cutoffCases` store: one active case per (paymentId, valueDate).

A case is the Cutoff Agent's record of one tagged, held payment on one business day. It keeps
the same caseId — and so the same LangGraph thread — across both phases of a day (C1: the
PENDING_APPROVAL reminder at 17:05 and the EXPEDITE after 18:15 are one case). The unique
partial index `idx_cutoff_case_active_unique` (transactions `ensure_indexes`) is the authority
for "one active"; `open_or_get` inserts and, on a lost race, re-reads the winner.

Timestamps: `openedAt`/`updatedAt` are real UTC (the TTL runs on the wall clock);
`businessOpenedAt` and `trigger.at` are the demo run's business time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

COLLECTION = "cutoffCases"
STATE_COLLECTION = "cutoffAgentState"
STREAM_STATE_ID = "cutoff-payments-stream"

OPEN = "OPEN"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
ACTIONED = "ACTIONED"
RESOLVED = "RESOLVED"
EXPIRED = "EXPIRED"

SOURCE_STREAM = "STREAM"
SOURCE_SWEEP = "SWEEP"
SOURCE_MANUAL = "MANUAL"

SUBMITTED_IN_TIME = "SUBMITTED_IN_TIME"
HELD_NEXT_VALUE_DATE = "HELD_NEXT_VALUE_DATE"
DEFERRED_NEXT_BUSINESS_DAY = "DEFERRED_NEXT_BUSINESS_DAY"
NO_ACTION_NEEDED = "NO_ACTION_NEEDED"
RELEASED = "RELEASED"
REJECTED = "REJECTED"
SUPERSEDED = "SUPERSEDED"
CLOCK_EXPIRED = "CLOCK_EXPIRED"

# The outcomes where the agent's approved action did the work; the rest closed around it.
_ACTIONED_RESULTS = {SUBMITTED_IN_TIME, HELD_NEXT_VALUE_DATE, DEFERRED_NEXT_BUSINESS_DAY}

LEASE_SECONDS = 120
RISK_HISTORY_MAX = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def trigger_key(status: Optional[str], risk: Optional[dict]) -> str:
    """What the case last ran on. A change of any part means the agent should look again."""
    risk = risk or {}
    return "|".join(str(v) for v in (status, risk.get("phase"), risk.get("riskLevel"),
                                     risk.get("blockerType")))


def get(db: Any, case_id: str) -> dict:
    return db[COLLECTION].find_one({"caseId": case_id}, {"_id": 0}) or {}


def active_for(db: Any, payment_id: str, value_date: str) -> Optional[dict]:
    return db[COLLECTION].find_one({"paymentId": payment_id, "valueDate": value_date,
                                    "active": True}, {"_id": 0})


def open_or_get(db: Any, *, payment: dict, risk: Optional[dict], value_date: str,
                business_at: datetime, source: str = SOURCE_SWEEP) -> dict:
    """The active case for this payment and value date, created if absent."""
    payment_id = payment.get("paymentId")
    existing = active_for(db, payment_id, value_date)
    if existing:
        return existing
    now = _now()
    demo = payment.get("demo") or {}
    doc = {
        "caseId": f"CUT-{ObjectId()}",
        "paymentId": payment_id,
        "clockRunId": demo.get("clockRunId"),
        "scenario": demo.get("scenario"),
        "valueDate": value_date,
        "status": OPEN,
        "active": True,
        "openedAt": now,
        "updatedAt": now,
        "businessOpenedAt": business_at,
        "trigger": {"key": trigger_key(payment.get("status"), risk), "source": source,
                    "at": business_at},
        "risk": risk,
        "riskHistory": [risk] if risk else [],
        "agent": {"actionsTaken": [], "rejectedActions": []},
        "outcome": None,
    }
    try:
        db[COLLECTION].insert_one(doc)
    except DuplicateKeyError:
        return active_for(db, payment_id, value_date) or {}
    doc.pop("_id", None)
    return doc


def set_fields(db: Any, case_id: str, fields: dict, *, only_active: bool = False) -> bool:
    """`only_active` makes the write a no-op on a closed case (a racing closer won); returns
    whether a case matched."""
    query: dict = {"caseId": case_id, "active": True} if only_active else {"caseId": case_id}
    result = db[COLLECTION].update_one(query, {"$set": {**fields, "updatedAt": _now()}})
    return result.matched_count > 0


def set_agent_fields(db: Any, case_id: str, fields: dict, push: Optional[dict] = None) -> None:
    """Merge `fields` into `agent{}` (and optionally `$push` onto its arrays)."""
    coll = db[COLLECTION]
    coll.update_one({"caseId": case_id, "agent": None}, {"$set": {"agent": {}}})
    update: dict = {"$set": {**{f"agent.{k}": v for k, v in fields.items()}, "updatedAt": _now()}}
    if push:
        update["$push"] = {f"agent.{k}": v for k, v in push.items()}
    coll.update_one({"caseId": case_id}, update)


def record_risk(db: Any, case_id: str, risk: Optional[dict]) -> None:
    if risk is None:
        return
    db[COLLECTION].update_one({"caseId": case_id}, {
        "$set": {"risk": risk, "updatedAt": _now()},
        "$push": {"riskHistory": {"$each": [risk], "$slice": -RISK_HISTORY_MAX}},
    })


def claim_lease(db: Any, case_id: str, owner: str, *, now: Optional[datetime] = None) -> bool:
    """Take the case for `owner` unless another live lease holds it. Re-claiming is allowed."""
    now = now or _now()
    claimed = db[COLLECTION].find_one_and_update(
        {"caseId": case_id, "$or": [{"agent.lease": None}, {"agent.lease.until": {"$lt": now}},
                                    {"agent.lease.owner": owner}]},
        {"$set": {"agent.lease": {"owner": owner, "until": now + timedelta(seconds=LEASE_SECONDS)}}},
        return_document=ReturnDocument.AFTER,
    )
    return claimed is not None


def release_lease(db: Any, case_id: str, owner: str) -> None:
    db[COLLECTION].update_one({"caseId": case_id, "agent.lease.owner": owner},
                              {"$set": {"agent.lease": None}})


def close(db: Any, case_id: str, *, result: str, payment_status: Optional[str] = None,
          value_date: Optional[str] = None, at: Optional[datetime] = None) -> bool:
    """End the case. It leaves the active index, so the payment may open a fresh one.
    A case already closed is left as it is (first closer wins); returns whether this closed it."""
    if result == CLOCK_EXPIRED:
        status = EXPIRED
    elif result in _ACTIONED_RESULTS:
        status = ACTIONED
    else:
        status = RESOLVED
    return set_fields(db, case_id, {
        "status": status, "active": False,
        "outcome": {"result": result, "paymentStatus": payment_status,
                    "valueDate": value_date, "at": at or _now()},
    }, only_active=True)
