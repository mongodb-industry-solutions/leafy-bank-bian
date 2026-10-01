"""Statement simulator worker — the camt.053 for each settlement account, every interval.

Reconciliation plan A1. Each cycle books every newly-settled outbound wire onto the next
statement (`statement.generate_statement`). Periodic, like `inbound_sim_worker`: the trigger
is time, the way a correspondent's end-of-day statement is.

On by default (`ENABLE_STATEMENT_SIM`, flipped 2026-10-01): the normal flow — the
correspondent's confirmation arrives on the cycle. With it off no statement arrives, which
is itself a demo state: leg 2 has no external record to confirm against (the timing-lag
case the agentic scenarios present).

Same 2026-07-08 rules as the inbound simulator: the loop always sleeps one interval, and
`run` never returns normally.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30  # demo pacing: a wire goes green within ~one cycle


# Both settlement accounts `settle._select_model` can route a wire to: 1111 (a correspondent
# nostro, for a cross-border wire) and 1121 (the central-bank account, for a domestic Fedwire
# wire). Each gets its own statement, as each has its own account servicer.
ACCOUNT_CODES = ("1111", "1121")


def generate_cycle(service) -> list:
    """One cycle: at most one statement per account. Returns those written."""
    docs = [service.generate_statement(account_code=code) for code in ACCOUNT_CODES]
    return [d for d in docs if d]


def run(service, interval: int = DEFAULT_INTERVAL_SECONDS) -> None:
    logger.info("statement_sim_worker starting — one camt.053 every %ds", interval)
    while True:
        try:
            for doc in generate_cycle(service):
                logger.info("statement_sim_worker: %s (%d lines)",
                            doc["paymentMessageId"], len(doc["entries"]))
        except Exception:
            logger.exception("statement_sim_worker error")
        time.sleep(interval)
