"""The inbound payment saga — the stage sequence for a received pacs.008.

The mirror of `payment_lifecycle.py`, and it holds the same three invariants: every stage is
`run(ctx) -> None`, stages never call each other, and only this module knows the order.

## Why a separate sequence rather than branches inside the outbound one

The two directions genuinely differ in *which stages exist*, not merely in what each stage
does. Inbound has no orchestration stage (nothing to route — her L823), no rail submission
(nothing to submit — her L938), and its stage 2 asks a different question entirely (message
legitimacy, not customer entitlement — her L488). Threading `if direction == INBOUND`
through eight outbound stages would put an inbound branch inside modules that have no
inbound meaning; a second list is smaller and says what it means.

What the two DO share is everything from the credit onward: stage 6's pipeline, stage 7's
settlement collection and stage 8's three-way match are reused unchanged, with only the leg
direction mirrored (FR-6.IN1 / FR-7.IN1 / FR-8.IN1). That sharing lives in the ledger
service and in `settlementPositions`, which never learn that inbound exists.

## Her stage numbering, preserved

    1 receive      FinancialGateway        DRAFT -> RECEIVED
    2 resolve      FinancialGateway        -> VALIDATED      (or UTA hold)
    3 screen       PaymentOrderInitiation  -> ENRICHED -> FINAL_VALIDATED
    4 accept       PaymentConfirmation     (decision; REJECT -> RJCT at stage 5)
    5 acknowledge  FinancialGateway        -> ACCEPTED       (pacs.002 ACCP)
                                           or REJECTED       (pacs.002 RJCT; stages 6-8 not reached)
    6 credit       FinancialAccounting     -> IN_PROGRESS    (Dr clearing / Cr customer)
    7 settle       PaymentSettlement       -> SETTLED        (on arrival, D-IN4)
    8 reconcile    AccountReconciliation   -> RECONCILED     (async, in the ledger)

Stages 3+4 share a module and 5+6+7 share one — see each module's docstring for why.
"""

from __future__ import annotations

import logging

from contexts.account_reconciliation import reconcile
from contexts.financial_gateway.application import apply, receive, resolve, screen_and_accept
from contexts.payment_order_initiation.domain import lifecycle
from process.payment_context import PaymentContext

logger = logging.getLogger(__name__)

STAGES = [
    ("1 receive",     receive.run),             # FinancialGateway     DRAFT -> RECEIVED
    ("2 resolve",     resolve.run),             # FinancialGateway     -> VALIDATED
    ("3 screen",      screen_and_accept.run),   # + 4 accept           -> FINAL_VALIDATED
    ("5 apply",       apply.run),               # + 6 credit + 7 settle -> SETTLED
    ("8 reconcile",   reconcile.run),           # AccountReconciliation (async, in ledger)
]


def run(ctx: PaymentContext, *, start_index: int = 0) -> dict:
    """Run the inbound lifecycle. Returns the persisted payment document.

    `start_index` re-enters the saga on a held payment — used by UTA Repair, which resumes
    at stage 3 (her L1336: *"the payment resumes at Stage 3"*) on a document whose
    beneficiary an operator has just corrected.

    A stage signals a business failure by raising `ValueError`; this marks the payment and
    re-raises, exactly as the outbound saga does. But note what inbound does NOT do: a
    beneficiary that cannot be resolved is **not** a ValueError — it is a UTA hold, because
    the funds are already here (see `resolve._fail`).
    """
    for label, stage in STAGES[start_index:]:
        try:
            stage(ctx)
        except ValueError as exc:
            _mark_rejected(ctx, label, exc)
            raise

        if ctx.halt:
            logger.info("inbound lifecycle halted at %s", label.strip())
            return ctx.result

    if ctx.result is None:
        raise RuntimeError(
            "inbound lifecycle completed without a result — `apply` must set ctx.result"
        )
    return ctx.result


def _mark_rejected(ctx: PaymentContext, label: str, exc: ValueError) -> None:
    """Record the terminal state, if there is a document to record it on. Never raises."""
    if ctx.payment_oid is None or ctx.current_state is None:
        # Raised before the payment was persisted — a malformed message never becomes a
        # payment, and the raw message is still stored for inspection (FR-1.IN1).
        return
    lifecycle.reject(
        ctx.collections.payments,
        ctx.payment_oid,
        reason=f"Rejected at inbound stage {label.strip()}: {exc}",
    )


# The index in `STAGES` that UTA Repair resumes from — stage 3, per her L1336. Named rather
# than written as a literal `2` at the call site, so the resume point survives any future
# reordering of the list above.
REPAIR_RESUME_INDEX = 2
