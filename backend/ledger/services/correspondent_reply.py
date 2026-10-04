"""The correspondent's answer to an escalated exception (the simulator's missing half).

`ESCALATE_TO_CORRESPONDENT` sends a camt.026 and leaves the exception OPEN with
`awaitingCounterparty`. Nothing outside the demo would answer it on a timer, so this module
plays the correspondent: after `reply_delay_seconds` (worker) or on demand (the Showcase
button) it answers the case.

- Amount discrepancy / missing line: the correspondent confirms the full amount. The position
  takes the expected amount as its actual, then the normal RECHECK runs — the deterministic
  tie-out still decides, and the exception closes only if it agrees.
- Orphaned line: the correspondent confirms a misposting. The exception is dismissed.

The ledger runs this because it owns the position and the exception's resolution.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from database.connection import MongoDBConnection
from services import resolution_service
from services.exceptions_service import (
    CATEGORY_ORPHANED_SETTLEMENT,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_RECONCILIATION_MISSING,
    STATUS_DISMISSED,
    STATUS_OPEN,
)

logger = logging.getLogger(__name__)

CORRESPONDENT = "correspondent-sim"
DEFAULT_REPLY_SECONDS = 20
_DISMISS = "DISMISS"


def _awaiting(exc_coll, exception_id: str) -> dict:
    exc = resolution_service._open_exception(
        exc_coll, exception_id,
        {CATEGORY_RECONCILIATION_DISCREPANCY, CATEGORY_RECONCILIATION_MISSING,
         CATEGORY_ORPHANED_SETTLEMENT}, "CORRESPONDENT_REPLY")
    if not exc.get("awaitingCounterparty"):
        raise resolution_service.Conflict(
            f"Exception {exception_id} has not been escalated to the correspondent.")
    return exc


def _stamp_reply(exc_coll, exc: dict, outcome: str, now: datetime) -> None:
    """Record the answer; conditional on still-OPEN-and-awaiting so a double trigger (worker
    plus button, or two deployments on this DB) replies once."""
    claim = exc_coll.update_one(
        {"_id": exc["_id"], "status": STATUS_OPEN, "awaitingCounterparty": True},
        {"$set": {"awaitingCounterparty": False, "escalation.reply": {
            "by": CORRESPONDENT, "at": now, "outcome": outcome}, "updatedAt": now}})
    if not claim.matched_count:
        raise resolution_service.Conflict(
            f"Exception {exc['exceptionId']} was answered by another caller.")


def reply(connection: MongoDBConnection, db_name: str, exception_id: str) -> dict:
    """Answer one escalated exception. Returns `{reply, exception}` (+ `outcome`, `check` from the recheck)."""
    exc_coll = connection.get_collection(db_name, "exceptions")
    exc = _awaiting(exc_coll, exception_id)
    now = datetime.now(timezone.utc)

    if exc["category"] == CATEGORY_ORPHANED_SETTLEMENT:
        _stamp_reply(exc_coll, exc, "MISPOSTING_CONFIRMED", now)
        exc_coll.update_one(
            {"_id": exc["_id"], "status": STATUS_OPEN},
            {"$set": {"status": STATUS_DISMISSED, "resolution": {
                "action": _DISMISS, "by": CORRESPONDENT, "at": now,
                "note": "Correspondent confirmed the line was a misposting."},
                "updatedAt": now}})
        return {"reply": "MISPOSTING_CONFIRMED",
                "exception": exc_coll.find_one({"exceptionId": exception_id}, {"_id": 0})}

    positions = connection.get_collection(db_name, "settlementPositions")
    position = positions.find_one({"paymentId": exc["paymentId"]}, sort=[("createdAt", -1)])
    if position is None:
        raise resolution_service.NotLegal(
            f"Payment {exc['paymentId']} has no settlement position to correct.")
    expected = position.get("expectedAmount", position.get("grossAmount"))
    positions.update_one({"_id": position["_id"]}, {"$set": {
        "actualAmount": expected,
        "actualCurrency": position.get("expectedCurrency", position.get("currency")),
        "actualBookedAt": now,
        "sourceMessageRef": (exc.get("escalation") or {}).get("paymentMessageId"),
    }})
    _stamp_reply(exc_coll, exc, "AMOUNT_CONFIRMED", now)
    result = resolution_service.recheck(
        connection, db_name, exception_id, by=CORRESPONDENT,
        note="Correspondent replied to the investigation request and confirmed the full amount.")
    return {"reply": "AMOUNT_CONFIRMED", **result}


def due_exception_ids(connection: MongoDBConnection, db_name: str, *,
                      delay_seconds: int = DEFAULT_REPLY_SECONDS,
                      now: Optional[datetime] = None) -> list[str]:
    """Escalated exceptions whose request is at least `delay_seconds` old."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=delay_seconds)
    exc_coll = connection.get_collection(db_name, "exceptions")
    return [e["exceptionId"] for e in exc_coll.find(
        {"status": STATUS_OPEN, "awaitingCounterparty": True, "escalation.at": {"$lte": cutoff}},
        {"exceptionId": 1})]


def reply_due(connection: MongoDBConnection, db_name: str, *,
              delay_seconds: int = DEFAULT_REPLY_SECONDS,
              now: Optional[datetime] = None) -> list[str]:
    """Answer every due escalation. Returns the ids answered; a lost race is skipped."""
    answered = []
    for exception_id in due_exception_ids(connection, db_name, delay_seconds=delay_seconds, now=now):
        try:
            reply(connection, db_name, exception_id)
            answered.append(exception_id)
        except resolution_service.ResolutionError as e:
            logger.info("correspondent reply skipped for %s: %s", exception_id, e)
    return answered
