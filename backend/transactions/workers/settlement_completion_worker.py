"""Settlement-completion worker — second half of Stage 7 for a MATCHED wire.

Periodic loop (not a change stream). `settle.run` captures a default wire at
`settlementStatus=PENDING` and stops the saga; this worker flips captured wires to `SETTLED`
once `clearing.submittedAt` is older than `SETTLEMENT_COMPLETION_DELAY_SECONDS` (default 30).
The deferred window is what makes the clearing-and-settlement stage visible on the list —
Doina (Sep 17): "wires should settle only when they have reached and completed the clearing &
settlement stage."

Built as a periodic scan (like `ledger/workers/gl_batch.py`), not a CDC worker, on purpose:
nothing updates a PENDING payment while it waits, so there is no change-stream event to react
to — the trigger is time, not a write. That also avoids the resume-token / oplog-rollover
class that bit the CDC workers (defects 2026-07-01).

The loop always sleeps one `interval` per cycle, even when `complete_due` completes nothing,
so it can never busy-loop on an empty scan (the 2026-07-08 `eod_topup_worker` failure mode).

Idempotency is `complete_due`'s own: completion flips `settlementStatus` to SETTLED, so the
next cycle's query cannot reselect a completed payment; a race between two cycles is closed by
`lifecycle.advance`'s `from_state=IN_PROGRESS` guard.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from contexts.payment_settlement import settle
from database.connection import MongoDBConnection

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 5
DEFAULT_DELAY = 30


def run(connection: MongoDBConnection, db_name: str,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        delay_seconds: float = DEFAULT_DELAY) -> None:
    """Run the completion loop. Each cycle calls `settle.complete_due` then sleeps."""
    logger.info(
        "settlement_completion_worker starting — poll=%ds, delay=%ss on %s",
        poll_interval, delay_seconds, db_name,
    )
    while True:
        try:
            completed = settle.complete_due(connection, db_name, delay_seconds=delay_seconds)
            if completed:
                logger.info(
                    "settlement_completion_worker: completed %d payment(s) this cycle",
                    completed,
                )
        except Exception:
            logger.exception("settlement_completion_worker error")
        time.sleep(poll_interval)


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI is not set")
    db_name = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")
    poll_interval = int(os.getenv("SETTLEMENT_COMPLETION_POLL_SECONDS", str(DEFAULT_POLL_INTERVAL)))
    delay_seconds = float(os.getenv("SETTLEMENT_COMPLETION_DELAY_SECONDS", str(DEFAULT_DELAY)))

    connection = MongoDBConnection(uri)
    run(connection, db_name, poll_interval=poll_interval, delay_seconds=delay_seconds)


if __name__ == "__main__":
    main()
