"""Drives the Reconciliation Agent: a change stream for new exceptions, a sweep for rechecks.

- **Change stream** on `exceptions`: inserts of OPEN `RECONCILIATION_DISCREPANCY`,
  `RECONCILIATION_MISSING` and `ORPHANED_SETTLEMENT`. Backfilled precedents (`historical`)
  are excluded — they are evidence, never queue work.
- **Sweep** every `RECON_AGENT_SWEEP_SECONDS` (60): OPEN exceptions whose `agent.nextCheckAt`
  has passed (the agent waiting out a timing lag), plus OPEN exceptions the agent has never
  looked at. The second set covers the change stream's in-memory resume token: events missed
  across a restart are picked up here instead of being lost.

Stream and sweep share one per-exception guard so the same exception is never investigated
by both at once.

## Error handling

Mirrors the ledger's learned pattern (defects 2026-07-01): the whole open-and-iterate cycle is
wrapped, so a `NonResumableChangeStreamError` raised mid-iteration gets the same clear-token-
and-reopen recovery. One bad event is logged and skipped rather than crash-looping.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo.errors import OperationFailure

import policy
from reconciliation_agent import investigate

logger = logging.getLogger(__name__)

_MATCH = [
    {"$match": {
        "operationType": "insert",
        "fullDocument.category": {"$in": list(policy.WATCHED_CATEGORIES)},
        "fullDocument.status": "OPEN",
        "fullDocument.historical": {"$in": [None]},
    }}
]

_REOPEN_BACKOFF_SECONDS = 5.0
# An exception younger than this is the change stream's to pick up; the sweep leaves it alone.
_UNSEEN_GRACE = timedelta(minutes=2)

_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


def _claim(exception_id: str) -> bool:
    with _in_flight_lock:
        if exception_id in _in_flight:
            return False
        _in_flight.add(exception_id)
        return True


def _release(exception_id: str) -> None:
    with _in_flight_lock:
        _in_flight.discard(exception_id)


def _run_one(doc: dict, agent: Any, db: Any) -> None:
    exception_id, payment_id = doc.get("exceptionId"), doc.get("paymentId")
    if not exception_id or not payment_id:
        logger.warning("reconciliation event missing ids: %s", doc)
        return
    if not _claim(exception_id):
        return
    try:
        logger.info("Reconciliation Agent invoked for %s (%s, %s)",
                    exception_id, doc.get("category"), payment_id)
        investigate(agent, db, exception_id, payment_id)
    except Exception:  # noqa: BLE001 — one exception must not kill the worker.
        logger.warning("investigate failed for %s — skipping", exception_id, exc_info=True)
    finally:
        _release(exception_id)


def sweep_query(now: datetime) -> dict:
    return {
        "category": {"$in": list(policy.WATCHED_CATEGORIES)},
        "status": "OPEN",
        "historical": None,
        "$or": [
            {"agent.nextCheckAt": {"$lte": now}},
            {"agent": None, "createdAt": {"$lte": now - _UNSEEN_GRACE}},
        ],
    }


def sweep_once(agent: Any, db: Any, now: datetime | None = None) -> int:
    docs = list(db["exceptions"].find(sweep_query(now or datetime.now(timezone.utc)),
                                      {"_id": 0, "exceptionId": 1, "paymentId": 1, "category": 1})
                .limit(25))
    for doc in docs:
        _run_one(doc, agent, db)
    return len(docs)


def run_sweep(agent: Any, db: Any) -> None:
    interval = float(os.getenv("RECON_AGENT_SWEEP_SECONDS", "60"))
    while True:
        try:
            sweep_once(agent, db)
        except Exception:  # noqa: BLE001
            logger.warning("reconciliation sweep error", exc_info=True)
        time.sleep(interval)


def run_reconciliation_worker(agent: Any, db: Any) -> None:
    """Open-and-iterate cycle with non-resumable recovery. Blocks; run in a daemon thread."""
    coll = db["exceptions"]
    resume_token = None
    while True:
        try:
            stream = coll.watch(_MATCH, resume_after=resume_token)
            for event in stream:
                resume_token = event.get("_id")
                _run_one(event.get("fullDocument") or {}, agent, db)
        except OperationFailure as exc:
            # Token aged out of the oplog: reopen from now. The sweep catches the gap.
            logger.warning("reconciliation change stream error (%s) — clearing token and reopening",
                           exc, exc_info=True)
            resume_token = None
            time.sleep(_REOPEN_BACKOFF_SECONDS)
        except Exception:  # noqa: BLE001 — never exit the loop.
            logger.warning("reconciliation worker iteration error — reopening", exc_info=True)
            time.sleep(_REOPEN_BACKOFF_SECONDS)


def start_reconciliation_worker(agent: Any, db: Any) -> list[threading.Thread]:
    """Start the stream and sweep threads, if enabled."""
    # ⚠️ Default OFF (2026-09-29, Kiran). It calls Bedrock per exception and writes to
    # documents on a change stream — an opt-in, not a default (handover §9).
    if os.getenv("ENABLE_RECONCILIATION_AGENT", "false").lower() not in ("1", "true", "yes"):
        logger.info("Reconciliation Agent worker disabled (ENABLE_RECONCILIATION_AGENT != true).")
        return []
    if agent is None:
        logger.info("Reconciliation Agent not built — worker not started.")
        return []
    threads = [
        threading.Thread(target=run_reconciliation_worker, args=(agent, db), daemon=True,
                         name="reconciliation-worker"),
        threading.Thread(target=run_sweep, args=(agent, db), daemon=True,
                         name="reconciliation-sweep"),
    ]
    for t in threads:
        t.start()
    logger.info("Reconciliation Agent started (stream on %s + recheck sweep).",
                ", ".join(policy.WATCHED_CATEGORIES))
    return threads
