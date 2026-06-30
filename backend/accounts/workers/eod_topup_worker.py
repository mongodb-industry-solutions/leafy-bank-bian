"""EOD top-up worker — credits ACTIVE accounts that fall below the minimum balance.

Scheduled loop: sleeps until midnight UTC (or a configurable interval for testing),
then bulk-increments any ACTIVE account whose available balance is below the threshold.
Uses update_many so the entire sweep is a single round-trip; each document update is
atomic at the MongoDB level.

Env vars (read by the caller in main.py and passed as arguments):
  EOD_TOPUP_THRESHOLD      — minimum balance that triggers a top-up (default 500)
  EOD_TOPUP_AMOUNT         — amount credited per top-up (default 500)
  EOD_TOPUP_INTERVAL_SECONDS — override sleep interval in seconds; 0 = sleep until midnight UTC
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from database.connection import MongoDBConnection

logger = logging.getLogger(__name__)


def _seconds_until_midnight_utc() -> float:
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()


def run_once(accounts_coll, threshold: float, amount: float) -> int:
    """Execute one top-up sweep. Returns the number of accounts credited."""
    now = datetime.now(timezone.utc)
    result = accounts_coll.update_many(
        {
            "status": "ACTIVE",
            "type": {"$in": ["CURRENT", "SAVINGS"]},
            "balance.available": {"$lt": threshold},
        },
        {
            "$inc": {
                "balance.current": amount,
                "balance.available": amount,
                # balance.ledger intentionally excluded — owned by the GL pipeline
            },
            "$set": {"balance.updatedAt": now, "updatedAt": now},
        },
    )
    if result.modified_count:
        logger.info("eod_topup: credited $%.0f to %d account(s)", amount, result.modified_count)
    else:
        logger.info("eod_topup: no accounts below threshold — nothing to do")
    return result.modified_count


def run(
    connection: MongoDBConnection,
    db_name: str,
    threshold: float = 500.0,
    amount: float = 500.0,
    interval: int = 0,
) -> None:
    """Top up ACTIVE accounts below `threshold` by `amount`.

    `interval` > 0 overrides the midnight-UTC sleep (useful for testing).
    """
    accounts = connection.get_collection(db_name, "accounts")
    logger.info(
        "eod_topup_worker starting — threshold=$%.0f top_up=$%.0f",
        threshold,
        amount,
    )

    while True:
        sleep_for = interval if interval > 0 else _seconds_until_midnight_utc()
        logger.info("eod_topup_worker: next run in %.0fs", sleep_for)
        time.sleep(sleep_for)

        try:
            run_once(accounts, threshold, amount)
        except Exception:
            logger.exception("eod_topup_worker error")
