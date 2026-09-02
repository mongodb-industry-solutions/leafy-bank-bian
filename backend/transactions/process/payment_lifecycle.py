"""The payment saga — the single place that knows the stage sequence.

Doina's nine-stage payment lifecycle (`doinas-research/Payemnt WorkFlow Tab 1.md`).
Structure and rationale: `11-stage-scaffold-plan.md`.

Three invariants make the stages movable (plan §6). Breaking any of them turns a later
file move into an untangling job:

1. Every stage is `run(ctx: PaymentContext) -> None`. This module knows names and order,
   nothing else — so a stage can later move to another service, behind HTTP, or behind a
   change stream without this file changing.
2. **Stages never call each other.** Only this module sequences them.
3. Each stage declares what it reads and writes on `ctx`, at the top of its file.

---

## The order is Doina's 1 -> 9

The instruction is persisted at `DRAFT` in stage 1 and advanced from there (plan §11), which
is what lets the sequence follow her doc exactly. `ROUTED` (stage 4a) precedes `AUTHORISED`
(stage 4b) because a destination must be resolved before it can be fraud-scored (D1) — her
own list omits `ROUTED`; ours inserts it.

## Rejection is handled here, not in the stages

A stage signals a business failure by raising `ValueError`. This module catches it, marks the
payment with a terminal state and the failing stage as the reason, and re-raises. Stages carry
no rejection bookkeeping, and compensation stays a saga concern (doc 09 §4).

A stage that raises before the document exists — the basic-field guards in `capture` — has
nothing to mark, so the error propagates untouched.
"""

from __future__ import annotations

import logging

from contexts.account_reconciliation import reconcile
from contexts.fraud_evaluation import evaluate
from contexts.party_authentication import authenticate
from contexts.payment_order_initiation.application import capture
from contexts.payment_order_initiation.domain import enrichment, lifecycle, validation
from contexts.payment_orchestration import orchestrate
from contexts.payment_rail import execute
from contexts.payment_settlement import settle
from process.payment_context import PaymentContext

logger = logging.getLogger(__name__)


# (stage label, callable). The label is what appears in logs and in the demo UI.
STAGES = [
    ("1 capture",      capture.run),        # PaymentOrderInitiation  DRAFT -> INITIATED
    ("2 authenticate", authenticate.run),   # PartyAuthentication     (gate, no state)
    ("3 validate",     validation.run),     # PaymentOrderInitiation  -> VALIDATED
    ("3 enrich",       enrichment.run),     # PaymentOrderInitiation  -> ENRICHED -> FINAL_VALIDATED
    ("4 orchestrate",  orchestrate.run),    # PaymentOrchestration    -> ROUTED
    ("4 authorize",    evaluate.run),       # FraudEvaluation         -> AUTHORISED -> APPROVED
    ("5 execute",      execute.run),        # PaymentRail             -> SUBMITTED -> IN_PROGRESS -> SETTLED
    # No handler by design: stage 6 runs in the LEDGER service, asynchronously. `gl_batch`
    # posts the journal and then writes `POSTED` + `refs.journalEntryId` back onto the
    # payment (doc 20 B1). Built as of stage 6 — the `None` means "not this service", not
    # "not implemented".
    ("6 account",      None),               # FinancialAccounting     -> POSTED, by the ledger via CDC
    ("7 settle",       settle.run),         # PaymentSettlement       (SETTLED fires in stage 5)
    ("8 reconcile",    reconcile.run),      # AccountReconciliation   -> RECONCILED        [stub]
    # 9 exceptions is not in the happy path - see `compensation.py`.
]


def run(ctx: PaymentContext) -> dict:
    """Run the payment lifecycle. Returns the persisted payment document.

    Raises `ValueError` on any validation failure; the caller maps it to HTTP 400.
    A stage may call `ctx.stop(result)` to end the saga early — used for idempotent replay.
    """
    for label, stage in STAGES:
        if stage is None:
            # Stage 6 (Accounting) has no call here by design. The ledger service derives
            # `ledgerEvents` from a change stream on `transactions`, so the payment path
            # writes no ledger data (2026-06-18 async-CDC decision). The money move in
            # stage 5 is what triggers it. Do not add a call — that would re-couple
            # accounting into the payment path.
            continue

        try:
            stage(ctx)
        except ValueError as exc:
            _mark_rejected(ctx, label, exc)
            raise

        if ctx.halt:
            logger.info("payment lifecycle halted at %s", label.strip())
            return ctx.result

    if ctx.result is None:
        raise RuntimeError(
            "payment lifecycle completed without a result — stage 5 (execute) must set ctx.result"
        )
    return ctx.result


def _mark_rejected(ctx: PaymentContext, label: str, exc: ValueError) -> None:
    """Record the terminal state, if there is a document to record it on.

    Never raises: the caller is re-raising the real business error, and a bookkeeping failure
    must not replace a useful message with a confusing one.
    """
    if ctx.payment_oid is None or ctx.current_state is None:
        # Raised by a stage-1 basic-field guard, before the instruction was persisted.
        # Doina's Stage 1 covers this: a malformed request never becomes a payment.
        return

    lifecycle.reject(
        ctx.collections.payments,
        ctx.payment_oid,
        reason=f"Rejected at stage {label.strip()}: {exc}",
    )
