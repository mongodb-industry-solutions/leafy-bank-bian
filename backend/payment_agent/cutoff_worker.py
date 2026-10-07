"""Drives the Cutoff Agent: a change stream on payment status, a sweep for everything else.

- **Sweep** every `CUTOFF_AGENT_SWEEP_SECONDS` (30): tagged payments (`demo.clockRunId`) held
  at PENDING_APPROVAL / PENDING_FUNDS / PENDING_SCREENING / CUTOFF_EXCEPTION with no hold or
  defer decision yet (limit 25). Each gets its active case (`open_or_get`) and an agent run
  when the case is new, when its `trigger.key` (status|phase|risk|blocker) differs from
  `trigger.ranKey` (the key of the last completed run), or when a failed run is due a retry
  (≤ 5 attempts, ≥ 60 s apart). Then active cases the world has moved past are closed,
  superseding a paused thread first.
- **Change stream** on `payments`: updates that touch `status`, `updateLookup`, the demo tag
  filtered here in Python (approved Q8). Each event runs a sweep scoped to that payment. The
  resume token is persisted in `cutoffAgentState` after each event and every 30 s while idle,
  so a restart neither replays hours of oplog nor loses the gap (the sweep covers it anyway).

The agent run is guarded twice: an in-process claim (stream and sweep threads) and the case
lease (a second instance on the shared database). `investigate` does not take the lease.

## Error handling

Mirrors `reconciliation_worker.py`: the whole open-and-iterate cycle is wrapped. An
`OperationFailure` (token aged out of the oplog) clears the persisted token and reopens from
now; any other error reopens with the token kept. One bad event is logged and skipped.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pymongo.errors import OperationFailure

import cutoff_agent
import cutoff_cases as cases
import cutoff_clock
import cutoff_risk as risk_mod

logger = logging.getLogger(__name__)

SWEEP_LIMIT = 25
STALE_LIMIT = 50
MAX_ERROR_ATTEMPTS = 5
ERROR_RETRY_AFTER_SECONDS = 60
IDLE_CHECKPOINT_SECONDS = 30.0
_REOPEN_BACKOFF_SECONDS = 5.0

# Mirrors transactions `cutoff_scenarios.SUPERSEDED_REASON` (payment_agent cannot import
# transactions): the reject reason a new demo run writes on the previous run's payments.
SUPERSEDED_REASON = "Superseded by a new demo run."

_DECIDED = ("HOLD_NEXT_VALUE_DATE", "NEXT_VALUE_DATE")
_SUBMITTED_OR_LATER = {"SUBMITTED", "IN_PROGRESS", "POSTED", "SETTLED", "RECONCILED"}

_MATCH = [{"$match": {"operationType": "update",
                      "updateDescription.updatedFields.status": {"$exists": True}}}]

_OWNER = f"cutoff-worker:{socket.gethostname()}:{os.getpid()}"

_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


def _claim(case_id: str) -> bool:
    with _in_flight_lock:
        if case_id in _in_flight:
            return False
        _in_flight.add(case_id)
        return True


def _release(case_id: str) -> None:
    with _in_flight_lock:
        _in_flight.discard(case_id)


@contextmanager
def case_lock(db: Any, case_id: str) -> Iterator[bool]:
    """Hold the in-process claim and the case lease; yields False (holding neither) when
    another thread or instance has the case. Used by the run, the closer and the approve route."""
    if not _claim(case_id):
        yield False
        return
    try:
        if not cases.claim_lease(db, case_id, _OWNER):
            yield False
            return
        try:
            yield True
        finally:
            cases.release_lease(db, case_id, _OWNER)
    finally:
        _release(case_id)


def enabled() -> bool:
    # Default OFF: it calls Bedrock and writes to transactions-owned views on a shared DB;
    # the flag must stay off on staging-dev (plan risk 4).
    return os.getenv("ENABLE_CUTOFF_AGENT", "false").lower() in ("1", "true", "yes")


def _utc(at: Any) -> Any:
    return at.replace(tzinfo=timezone.utc) if isinstance(at, datetime) and at.tzinfo is None else at


def candidate_query(payment_id: Optional[str] = None) -> dict:
    query = {"demo.clockRunId": {"$exists": True, "$ne": None},
             "status": {"$in": list(risk_mod.BLOCKER_FOR)},
             "cutoff.decision": {"$nin": list(_DECIDED)},
             # Release (transactions) owns the payment once it starts; no new case or Bedrock run.
             "demo.release": {"$exists": False}}
    if payment_id:
        query["paymentId"] = payment_id
    return query


def _retry_due(error: Optional[dict], real_now: datetime) -> bool:
    if not error or error.get("attempts", 0) >= MAX_ERROR_ATTEMPTS:
        return False
    at = _utc(error.get("at"))
    return isinstance(at, datetime) and at <= real_now - timedelta(seconds=ERROR_RETRY_AFTER_SECONDS)


def _needs_run(case: dict, key: str, real_now: datetime) -> bool:
    """`trigger.ranKey` is the key the agent last actually ran on, so a run that was blocked
    (claim, lease, paused for approval) leaves the change pending for the next sweep."""
    agent = case.get("agent") or {}
    error = agent.get("error")
    if error:
        return _retry_due(error, real_now)
    return (case.get("trigger") or {}).get("ranKey") != key


def _run_case(agent: Any, db: Any, case_id: str, payment_id: str, key: str) -> bool:
    with case_lock(db, case_id) as held:
        # A paused thread is not re-run; leaving ranKey stale means a REJECT followed by a
        # key change gets a fresh run on the next sweep.
        if not held or cutoff_agent.is_awaiting_approval(agent, case_id):
            return False
        logger.info("Cutoff Agent invoked for %s (%s)", case_id, payment_id)
        if cutoff_agent.investigate(agent, db, case_id, payment_id) is None:
            return False  # Failed: agent.error drives the retry.
        cases.set_fields(db, case_id, {"trigger.ranKey": key})
        return True


def _evaluate(agent: Any, db: Any, payment: dict, source: str, real_now: datetime) -> bool:
    """Open or refresh the payment's case; True when the agent ran."""
    payment_id = payment.get("paymentId")
    snap = cutoff_agent._snapshot(db, payment_id)
    if snap["at"] is None or snap["risk"] is None:
        return False  # Run gone (the stale pass closes the case) or no cut-off window applies.
    value_date = cutoff_clock.business_date(cutoff_clock.run(db, snap["runId"]), snap["at"]).isoformat()
    case = cases.open_or_get(db, payment=snap["payment"], risk=snap["risk"],
                             value_date=value_date, business_at=snap["at"], source=source)
    case_id = case.get("caseId")
    key = cases.trigger_key(snap["status"], snap["risk"])
    if not case_id or not _needs_run(case, key, real_now):
        return False
    cases.set_fields(db, case_id, {"trigger.key": key, "trigger.source": source,
                                   "trigger.at": snap["at"]})
    return _run_case(agent, db, case_id, payment_id, key)


def _last_reason(payment: dict) -> Optional[str]:
    events = (payment.get("lifecycle") or {}).get("events") or []
    return events[-1].get("reason") if events else None


def stale_outcome(db: Any, case: dict) -> Optional[str]:
    """The outcome for an active case the world has moved past, else None (still live)."""
    if cutoff_clock.now(db, case.get("clockRunId")) is None:
        return cases.CLOCK_EXPIRED
    payment = db["payments"].find_one({"paymentId": case.get("paymentId")}, {"_id": 0})
    if not payment:
        return cases.SUPERSEDED
    status = payment.get("status")
    decision = (payment.get("cutoff") or {}).get("decision")
    rejected = status == "REJECTED"
    if rejected and _last_reason(payment) == SUPERSEDED_REASON:
        return cases.SUPERSEDED
    # A recorded decision beats a plain REJECTED so a release that fails validation cannot
    # flip the case away from its HELD / DEFERRED story.
    if decision == "HOLD_NEXT_VALUE_DATE":
        return cases.HELD_NEXT_VALUE_DATE
    if decision == "NEXT_VALUE_DATE":
        return cases.DEFERRED_NEXT_BUSINESS_DAY
    if rejected:
        return cases.REJECTED
    if status in _SUBMITTED_OR_LATER:
        recommendation = ((case.get("agent") or {}).get("recommendation") or {}).get("kind")
        if not decision and recommendation == "NONE":
            return cases.NO_ACTION_NEEDED
        return cases.SUBMITTED_IN_TIME
    if status in risk_mod.BLOCKER_FOR:
        return None
    return cases.RELEASED


def _close_stale(agent: Any, db: Any, payment_id: Optional[str]) -> int:
    query: dict = {"active": True}
    if payment_id:
        query["paymentId"] = payment_id
    closed = 0
    for case in list(db[cases.COLLECTION].find(query, {"_id": 0}).limit(STALE_LIMIT)):
        result = stale_outcome(db, case)
        case_id = case.get("caseId")
        if result is None:
            continue
        try:
            with case_lock(db, case_id) as held:
                if not held:
                    continue
                cutoff_agent.supersede(agent, db, case_id, result=result,
                                       note=f"Closed by the cutoff worker: {result}.")
                closed += 1
        except Exception:  # noqa: BLE001 — one case must not stop the pass.
            logger.warning("closing cutoff case %s failed", case_id, exc_info=True)
    return closed


def sweep_once(agent: Any, db: Any, *, payment_id: Optional[str] = None,
               source: str = cases.SOURCE_SWEEP, real_now: Optional[datetime] = None) -> dict:
    """One pass: evaluate held tagged payments, then close stale cases."""
    real_now = real_now or datetime.now(timezone.utc)
    payments = list(db["payments"].find(candidate_query(payment_id),
                                        {"_id": 0, "paymentId": 1}).limit(SWEEP_LIMIT))
    investigated = 0
    for payment in payments:
        try:
            investigated += _evaluate(agent, db, payment, source, real_now)
        except Exception:  # noqa: BLE001 — one payment must not stop the pass.
            logger.warning("cutoff evaluate failed for %s", payment.get("paymentId"), exc_info=True)
    closed = _close_stale(agent, db, payment_id)
    return {"evaluated": len(payments), "investigated": investigated, "closed": closed}


def run_sweep(agent: Any, db: Any) -> None:
    interval = float(os.getenv("CUTOFF_AGENT_SWEEP_SECONDS", "30"))
    while True:
        try:
            sweep_once(agent, db)
        except Exception:  # noqa: BLE001
            logger.warning("cutoff sweep error", exc_info=True)
        time.sleep(interval)


# --- change stream -----------------------------------------------------------------------

def iter_with_idle_checkpoint(
    stream: Any,
    checkpoint: Callable[[Any], None],
    interval: float = IDLE_CHECKPOINT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[dict]:
    """Yield change events; on idle polls, persist `stream.resume_token` at most every
    `interval` s. Local copy of `ledger/shared/change_stream.py` (payment_agent cannot import
    ledger). The idle token never moves past an unprocessed event: `try_next()` returns None
    only once the batch is drained, and the generator resumes only after the caller's body."""
    last_checkpoint = clock()
    while stream.alive:
        change = stream.try_next()
        if change is not None:
            yield change
            last_checkpoint = clock()
            continue
        token = stream.resume_token
        if token is not None and clock() - last_checkpoint >= interval:
            checkpoint(token)
            last_checkpoint = clock()


def load_token(db: Any) -> Any:
    doc = db[cases.STATE_COLLECTION].find_one({"_id": cases.STREAM_STATE_ID}) or {}
    return doc.get("resumeToken")


def save_token(db: Any, token: Any) -> None:
    db[cases.STATE_COLLECTION].update_one(
        {"_id": cases.STREAM_STATE_ID},
        {"$set": {"resumeToken": token, "updatedAt": datetime.now(timezone.utc)}}, upsert=True)


def handle_event(agent: Any, db: Any, event: dict) -> None:
    doc = event.get("fullDocument") or {}
    if not (doc.get("demo") or {}).get("clockRunId") or not doc.get("paymentId"):
        return
    try:
        sweep_once(agent, db, payment_id=doc["paymentId"], source=cases.SOURCE_STREAM)
    except Exception:  # noqa: BLE001 — one bad event is skipped, not crash-looped.
        logger.warning("cutoff event failed for %s — skipping", doc.get("paymentId"), exc_info=True)


def run_cutoff_stream(agent: Any, db: Any, *, once: bool = False) -> None:
    """Open-and-iterate cycle with non-resumable recovery. Blocks; run in a daemon thread.
    `once` (tests) returns after the first cycle instead of reopening."""
    coll = db["payments"]
    while True:
        try:
            with coll.watch(_MATCH, full_document="updateLookup",
                            resume_after=load_token(db)) as stream:
                for event in iter_with_idle_checkpoint(stream, lambda t: save_token(db, t)):
                    handle_event(agent, db, event)
                    save_token(db, event.get("_id"))
        except OperationFailure as exc:
            logger.critical("cutoff change stream error (%s) — clearing token and reopening; "
                            "the sweep covers the gap", exc, exc_info=True)
            save_token(db, None)
            if not once:
                time.sleep(_REOPEN_BACKOFF_SECONDS)
        except Exception:  # noqa: BLE001 — never exit the loop.
            logger.warning("cutoff worker iteration error — reopening", exc_info=True)
            if not once:
                time.sleep(_REOPEN_BACKOFF_SECONDS)
        if once:
            return


def start_cutoff_worker(agent: Any, db: Any) -> list[threading.Thread]:
    """Start the stream and sweep threads, if enabled."""
    if not enabled():
        logger.info("Cutoff Agent worker disabled (ENABLE_CUTOFF_AGENT != true).")
        return []
    if agent is None:
        logger.info("Cutoff Agent not built — worker not started.")
        return []
    threads = [
        threading.Thread(target=run_cutoff_stream, args=(agent, db), daemon=True,
                         name="cutoff-stream"),
        threading.Thread(target=run_sweep, args=(agent, db), daemon=True, name="cutoff-sweep"),
    ]
    for t in threads:
        t.start()
    logger.info("Cutoff Agent started (payments status stream + %ss sweep).",
                os.getenv("CUTOFF_AGENT_SWEEP_SECONDS", "30"))
    return threads
