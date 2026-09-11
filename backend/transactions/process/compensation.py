"""Stage 9 — exceptions, repairs, and returns.  [NOT IMPLEMENTED]

**No BIAN Service Domain**, verified against the v14 landscape: no rows for Exception,
Repair, or Return. That is the answer, not a gap in the search — and it confirms doc 09 §4.
Compensation is a *saga* concern, not a domain one, which is why this file sits in
`process/` beside `payment_lifecycle.py` rather than under `contexts/`.

Put it in a context and every context grows its own retry mechanism.

Doina's rule is the whole design: *"repairs, recalls, returns, and retries should create
additional execution artifacts rather than overwrite the original."* Get `paymentExecutions`
append-only (plan §8 item 4) and most of this stage falls out for free — a return is a new
execution artifact, not a mutation of the old one.

TODO (Doina stage 9):
  - the six lifecycle terminal states (D1) and which stage failure maps to which.
  - repair: a payment that failed validation downstream, corrected and resubmitted — a new
    execution artifact against the same `payments` doc.
  - recall / return: an inbound reversal against a settled payment. Never a balance
    rewrite; always a compensating movement.
  - retry with bounded attempts. Note the prevention rule in defects.md (2026-07-01): never
    retry unboundedly, and distinguish permanent failures from transient ones.
  - compensation for a partial saga failure — today the only multi-write step is stage 5's
    ACID block, which rolls back on its own, so there is nothing to compensate yet. That
    changes the moment stage 4a starts writing `routingSnapshots` (and stage 4b stamps
    `payments.order`) before stage 5 runs.
"""

from __future__ import annotations

from process.payment_context import PaymentContext


def compensate(ctx: PaymentContext, failed_stage: str, error: Exception) -> None:
    """Not wired into `payment_lifecycle.run` yet — see the module docstring."""
    raise NotImplementedError("stage 9 compensation — plan §8, after paymentExecutions")
