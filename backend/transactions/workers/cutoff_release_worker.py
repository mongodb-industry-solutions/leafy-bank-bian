"""Cutoff release janitor — closes `CUTOFF-DEMO-` payments a presenter walked away from.

Plan-cutoff-release R3. Each cycle calls `cutoff_release.sweep_stale`: any demo run older than
`CUTOFF_RELEASE_AFTER_SECONDS` that still has an open payment is fast-forwarded and released,
through the same code path as the presenter's button.

Default OFF (`ENABLE_CUTOFF_RELEASE_JANITOR`), on in staging-dev only. Every deployment shares
one database, so a laptop with the flag on would release a presenter's live run; the claim in
`cutoff_release` keeps two janitors from double-acting but not from acting early.

Same 2026-07-08 rules as the other workers: no disabled branch inside `run` (the flag keeps
it out of `_restart_loop` in `main.py`), the loop always sleeps one interval, and `run` never
returns normally.
"""

from __future__ import annotations

import logging
import os
import time

from contexts.payment_orchestration.application import cutoff_release

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_RELEASE_AFTER_SECONDS = 1200
SWEEP_LIMIT = 25


def enabled() -> bool:
    return os.getenv("ENABLE_CUTOFF_RELEASE_JANITOR", "false").lower() == "true"


def release_after_seconds() -> int:
    return int(os.getenv("CUTOFF_RELEASE_AFTER_SECONDS", str(DEFAULT_RELEASE_AFTER_SECONDS)))


def sweep_cycle(service, release_after: int) -> list:
    """One cycle: release every abandoned run. Returns the per-run results."""
    return cutoff_release.sweep_stale(
        service, older_than_seconds=release_after, limit=SWEEP_LIMIT)


def run(service, interval: int = DEFAULT_INTERVAL_SECONDS,
        release_after: int = DEFAULT_RELEASE_AFTER_SECONDS) -> None:
    logger.info("cutoff_release_worker starting — sweep every %ds, release runs older than %ds",
                interval, release_after)
    while True:
        try:
            for result in sweep_cycle(service, release_after):
                logger.info("cutoff_release_worker: released run %s (%d payments)",
                            result["runId"], len(result["payments"]))
        except Exception:
            logger.exception("cutoff_release_worker error")
        time.sleep(interval)
