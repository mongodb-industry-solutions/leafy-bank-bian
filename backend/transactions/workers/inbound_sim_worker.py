"""Inbound simulator worker — two simulated incoming pacs.008s per interval.

The demo's ambient inbound traffic (Kiran, 2026-09-29, revised same day): every
`INBOUND_SIM_INTERVAL_SECONDS` (default 300 = 5 minutes), exactly `PAYMENTS_PER_CYCLE`
(default 2) **happy-path** payments arrive. Periodic loop, like
`settlement_completion_worker` — there is no external trigger to react to, the trigger IS
time. main.py passes the same `PaymentsService` instance the routes use, so the worker and
the manual route share one code path (`simulate_inbound`) and cannot drift apart.

## Happy path only, by rule (Kiran, 2026-09-29)

Ambient traffic NEVER opens exceptions and never fires an agent: an unattended simulator
queuing operator work is a queue that fills itself. The scenario variants that hold or
refuse a payment (MISMATCH, SANCTIONS) stay available on the MANUAL trigger and its API
(`POST /FinancialGateway/{id}/Inbound/Simulate`), where a human chose to create the work.

## The 2026-07-08 rules, honoured by construction

The loop ALWAYS sleeps one interval per cycle, whatever happened, so it can never busy-loop;
and `run` never returns normally — there is no "disabled" branch inside it, because main.py
gates whether the thread starts at all (`ENABLE_INBOUND_SIM`), so a disabled worker never
enters `_restart_loop` in the first place.
"""

from __future__ import annotations

import logging
import time

from contexts.financial_gateway.domain import simulate

logger = logging.getLogger(__name__)

# The requested cadence (Kiran, 2026-09-29): one cycle every 5 minutes.
DEFAULT_INTERVAL_SECONDS = 300

# How many payments one cycle generates. Exactly two (Kiran, 2026-09-29) — a number, not
# a range and not a draw, so the demo's traffic is predictable: every 5 minutes, two
# inbound wires land and settle.
PAYMENTS_PER_CYCLE = 2


def generate_cycle(service, count: int = PAYMENTS_PER_CYCLE) -> list:
    """One ambient cycle: `count` happy-path inbound payments. Returns the payment docs.

    Separated from `run` so the cycle's exact behaviour (count, scenario, no exceptions)
    is testable hermetically without running an infinite loop.
    """
    payments = []
    for _ in range(count):
        payments.append(service.simulate_inbound(simulate.SCENARIO_HAPPY))
    return payments


def run(service, interval: int = DEFAULT_INTERVAL_SECONDS) -> None:
    """Run the simulator loop. Each cycle generates the ambient payments then sleeps.

    `service` is the `PaymentsService` instance main.py already constructed — the same
    object the routes use. Never returns: a `return` here would hand control back to
    `_restart_loop`, which (since the 2026-07-08 fix) exits instead of re-invoking —
    silently stopping the demo's inbound traffic. There is no exit condition on purpose.
    """
    logger.info(
        "inbound_sim_worker starting — %d happy-path pacs.008 every %ds",
        PAYMENTS_PER_CYCLE, interval,
    )
    while True:
        try:
            for payment in generate_cycle(service):
                logger.info(
                    "inbound_sim_worker: HAPPY -> %s (%s)",
                    payment.get("paymentId"), payment.get("status"),
                )
        except Exception:
            # One failed cycle must not stop the demo's traffic — log and continue. A
            # ValueError here (no seedable account, no named holder) is a data gap the
            # manual route surfaces properly; the loop just retries next interval.
            logger.exception("inbound_sim_worker error")
        time.sleep(interval)
