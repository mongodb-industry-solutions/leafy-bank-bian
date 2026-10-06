"""Operational records for the demo-clock holds (cutoff plan A3).

`approvalRequests` and `screeningQueue` are views the cutoff agent reads. The hold state on
`payments` is the source of truth, so every write here is best-effort: a failure is logged and
never breaks the money path (the same rule as `business_clock`).

Duck-typed on `db` (anything indexable by collection name), so tests pass a FakeDb.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from shared import business_clock

logger = logging.getLogger(__name__)

APPROVAL_REQUESTS = "approvalRequests"
SCREENING_QUEUE = "screeningQueue"
STAFF_DIRECTORY = "staffDirectory"

OPEN = "OPEN"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
CLEARED = "CLEARED"
HIT = "HIT"
CANCELLED = "CANCELLED"


def _utc(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def on_shift(staff: dict, at: datetime) -> bool:
    """Is this person working at business time `at`? Derived, never stored (Q9)."""
    ooo = staff.get("outOfOfficeUntil")
    if ooo is not None and _utc(at) < _utc(ooo):
        return False
    shift = staff.get("shift") or {}
    if not shift.get("startEt") or not shift.get("endEt"):
        return False
    local = business_clock.to_et(_utc(at)).time()
    return _hhmm(shift["startEt"]) <= local < _hhmm(shift["endEt"])


def _run_id(payment: dict) -> Optional[str]:
    return (payment.get("demo") or {}).get("clockRunId")


def _insert_open(coll, doc: dict) -> dict:
    """Insert one OPEN record; on the unique-partial collision return the existing one.

    Insert-then-catch, never check-then-insert: the unique index serialises racing writers.
    """
    try:
        coll.insert_one(doc)
        return doc
    except DuplicateKeyError:
        return coll.find_one({"paymentId": doc["paymentId"], "status": OPEN})


def open_approval_request(db, *, payment: dict, at: datetime) -> Optional[dict]:
    try:
        account_id = (payment.get("debtor") or {}).get("accountId")
        account = db["accounts"].find_one({"accountId": account_id}) or {}
        initiator = payment.get("customerId")
        required = [s.get("customerId") for s in account.get("signatories") or []
                    if s.get("customerId") and s.get("customerId") != initiator]
        # A small directory: filter in Python rather than lean on array-element matching.
        primary = next((s.get("staffId") for s in db[STAFF_DIRECTORY].find({"role": "APPROVER"})
                        if account_id in (s.get("primaryFor") or [])), None)
        doc = {
            "approvalRequestId": f"APR-{ObjectId()}",
            "paymentId": payment.get("paymentId"),
            "accountId": account_id,
            "amount": payment.get("instructedAmount"),
            "currency": payment.get("instructedCurrency"),
            "requiredApprovers": required,
            "primaryApprover": primary,
            "assignedTo": primary,
            "requestedAt": at,
            "reminders": [],
            "escalatedTo": None,
            "status": OPEN,
            "resolvedBy": None,
            "resolvedAt": None,
            "synthetic": False,
            "demo": {"clockRunId": _run_id(payment)},
        }
        return _insert_open(db[APPROVAL_REQUESTS], doc)
    except Exception:  # noqa: BLE001 - best-effort view; see the module docstring
        logger.exception("could not open approval request for %s", payment.get("paymentId"))
        return None


def _close(db, collection: str, *, payment_id: str, status: str, by: str, at: datetime) -> None:
    try:
        db[collection].update_one(
            {"paymentId": payment_id, "status": OPEN},
            {"$set": {"status": status, "resolvedBy": by, "resolvedAt": at}},
        )
    except Exception:  # noqa: BLE001 - best-effort view; see the module docstring
        logger.exception("could not close %s record for %s", collection, payment_id)


def close_approval_request(db, *, payment_id: str, status: str, by: str, at: datetime) -> None:
    _close(db, APPROVAL_REQUESTS, payment_id=payment_id, status=status, by=by, at=at)


def enqueue_screening(db, *, payment: dict, reason: str, matched: Optional[str], at: datetime,
                      synthetic: bool = False, queued_at: Optional[datetime] = None,
                      priority: int = 0) -> Optional[dict]:
    try:
        analysts = sorted(db[STAFF_DIRECTORY].find({"role": "ANALYST"}),
                          key=lambda s: s.get("staffId") or "")
        assigned = next((s.get("staffId") for s in analysts if on_shift(s, at)), None)
        doc = {
            "screeningItemId": f"SQ-{ObjectId()}",
            "paymentId": payment.get("paymentId"),
            "queuedAt": queued_at or at,
            "priority": priority,
            "assignedAnalyst": assigned,
            "status": OPEN,
            "reason": reason,
            "matched": matched,
            "resolvedBy": None,
            "resolvedAt": None,
            "synthetic": synthetic,
            "demo": {"clockRunId": _run_id(payment)},
            # Real time, not business time: the TTL on synthetic items runs on the wall clock.
            "createdAt": datetime.now(timezone.utc),
        }
        return _insert_open(db[SCREENING_QUEUE], doc)
    except Exception:  # noqa: BLE001 - best-effort view; see the module docstring
        logger.exception("could not enqueue screening item for %s", payment.get("paymentId"))
        return None


def close_screening_item(db, *, payment_id: str, status: str, by: str, at: datetime) -> None:
    _close(db, SCREENING_QUEUE, payment_id=payment_id, status=status, by=by, at=at)


def screening_position(db, *, payment_id: str, at: datetime) -> Optional[dict]:
    """1-based queue position among OPEN items of the same clock run. Computed, never stored.

    Ahead = higher priority, or same priority and queued earlier. Run-scoped so concurrent
    presenters never see each other's queues. `at` is business time, for `analystsOnShift`.
    """
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
    return {"position": ahead + 1, "ahead": ahead, "analystsOnShift": on}
