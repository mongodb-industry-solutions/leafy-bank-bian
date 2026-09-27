"""Change-stream worker that drives the Reconciliation Agent.

Watches `exceptions` for inserts of OPEN `RECONCILIATION_DISCREPANCY` records — the exact
records the ledger service's `_stamp_discrepant` creates when deterministic reconciliation
flags a mismatch (reconciliation_service.py:434-480). On each insert, it invokes the
Reconciliation Agent with the exception's `paymentId` + `exceptionId`; the agent investigates
and writes its findings onto `exceptions.agent{}`.

## Error handling

Mirrors the ledger service's learned pattern (defects 2026-07-01): the *entire* open-and-
iterate cycle is wrapped so `NonResumableChangeStreamError` raised mid-iteration (not just at
open) gets the same clear-token-and-reopen recovery. A single bad event is logged and skipped
rather than crash-looping the worker. The worker is optional (env-gated) so the service boots
even on a cluster where change streams are unavailable.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from pymongo.errors import OperationFailure

from reconciliation_agent import investigate

logger = logging.getLogger(__name__)

# Match only inserts of OPEN reconciliation discrepancies — the records `_stamp_discrepant`
# produces. Watching inserts (not updates) means each new mismatch triggers one investigation;
# a re-opened duplicate (the resolve-never-sticks loop, defect B1) is a different event.
_MATCH = [
    {"$match": {
        "operationType": "insert",
        "fullDocument.category": "RECONCILIATION_DISCREPANCY",
        "fullDocument.status": "OPEN",
    }}
]

# How long to back off before reopening after a non-resumable error, so a wedged cluster does
# not spin a tight loop.
_REOPEN_BACKOFF_SECONDS = 5.0


def _process(event: dict, agent: Any, db: Any) -> None:
    doc = event.get("fullDocument") or {}
    exception_id = doc.get("exceptionId")
    payment_id = doc.get("paymentId")
    if not exception_id or not payment_id:
        logger.warning("reconciliation event missing ids: %s", doc)
        return
    logger.info("Reconciliation Agent invoked for %s (payment %s)", exception_id, payment_id)
    try:
        investigate(agent, db, exception_id, payment_id)
    except Exception:  # noqa: BLE001 — one event must not kill the worker.
        logger.warning(
            "investigate failed for %s — skipping this event", exception_id, exc_info=True,
        )


def run_reconciliation_worker(agent: Any, db: Any, connection: Any) -> None:
    """Open-and-iterate cycle with non-resumable recovery. Blocks; run in a daemon thread."""
    coll = db["exceptions"]
    resume_token = None
    while True:
        try:
            stream = coll.watch(_MATCH, resume_after=resume_token, full_document="updateLookup")
            for event in stream:
                resume_token = event.get("_id")
                _process(event, agent, db)
        except OperationFailure as exc:
            # NonResumableChangeStreamError (a subclass) mid-iteration: the resume token has
            # aged out of the oplog window. Clear it and reopen from now, per defect 2026-07-01.
            # A data loss here is acceptable for a demo agent — the exception is still OPEN and
            # the operator can still investigate manually or re-trigger via the HTTP route.
            logger.warning(
                "reconciliation change stream error (%s) — clearing token and reopening",
                exc, exc_info=True,
            )
            resume_token = None
            time.sleep(_REOPEN_BACKOFF_SECONDS)
        except Exception:  # noqa: BLE001 — never exit the loop.
            logger.warning("reconciliation worker iteration error — reopening", exc_info=True)
            time.sleep(_REOPEN_BACKOFF_SECONDS)


def start_reconciliation_worker(agent: Any, db: Any, connection: Any) -> threading.Thread | None:
    """Start the worker in a daemon thread, if enabled. Returns the thread or None."""
    if os.getenv("ENABLE_RECONCILIATION_AGENT", "true").lower() not in ("1", "true", "yes"):
        logger.info("Reconciliation Agent worker disabled (ENABLE_RECONCILIATION_AGENT != true).")
        return None
    if agent is None:
        logger.info("Reconciliation Agent not built — worker not started.")
        return None
    thread = threading.Thread(
        target=run_reconciliation_worker,
        args=(agent, db, connection),
        daemon=True,
        name="reconciliation-worker",
    )
    thread.start()
    logger.info("Reconciliation Agent worker started (watching exceptions for OPEN discrepancies).")
    return thread
