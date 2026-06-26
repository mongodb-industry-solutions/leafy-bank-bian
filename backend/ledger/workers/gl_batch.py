"""GL batch worker — Stage ③: subLedgerEntries → journalEntries.

Scheduled loop: every GL_BATCH_INTERVAL_SECONDS seconds, groups pending
subLedgerEntries by groupId, writes one balanced journalEntry per group, and
flips postingStatus=POSTED on ledgerEvents + subLedgerEntries.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from database.connection import MongoDBConnection
from services.journal_service import run_batch
from services.reconciliation_service import reconcile_all_accounts
from shared.coa_cache import ChartOfAccounts

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 600


def _reconcile(connection: MongoDBConnection, db_name: str) -> bool:
    """Return True if all accounts reconcile, False if any break detected.

    Fails open on exception (returns True) so a transient DB error does not
    permanently block the batch loop.
    """
    period_code = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        results = reconcile_all_accounts(connection, db_name, period_code=period_code)
        failed = [r for r in results if not r.is_reconciled]
        if failed:
            logger.warning(
                "reconciliation BREAK: %d/%d account(s) FAILED for %s — batch will be skipped",
                len(failed), len(results), period_code,
            )
            return False
        logger.info(
            "reconciliation OK: %d account(s) checked for %s",
            len(results), period_code,
        )
        return True
    except Exception:
        logger.exception("reconciliation error — batch proceeds (fail-open)")
        return True


def run(connection: MongoDBConnection, db_name: str, coa: ChartOfAccounts,
        interval: int = DEFAULT_INTERVAL) -> None:
    logger.info("gl_batch starting — interval=%ds on %s", interval, db_name)
    while True:
        try:
            if not _reconcile(connection, db_name):
                logger.warning("gl_batch skipped this cycle due to pre-batch reconciliation break")
            else:
                written = run_batch(connection, db_name, coa=coa)
                if written:
                    logger.info("gl_batch: posted %d journal(s)", written)
        except Exception:
            logger.exception("gl_batch error")
        time.sleep(interval)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI is not set")
    db_name = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")
    interval = int(os.getenv("GL_BATCH_INTERVAL_SECONDS", str(DEFAULT_INTERVAL)))

    connection = MongoDBConnection(uri)
    coa = ChartOfAccounts.from_db(connection, db_name)
    logger.info("CoA loaded: %d accounts", len(coa))

    run(connection, db_name, coa, interval)


if __name__ == "__main__":
    main()
