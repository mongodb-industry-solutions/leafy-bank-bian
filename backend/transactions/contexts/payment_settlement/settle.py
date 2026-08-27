"""Stage 7 — clearing & settlement.  [NOT IMPLEMENTED HERE — see below]

BIAN PaymentSettlement (SD 40033, no published semantic API) + InternalBankAccount
(SD 29306) for the nostro/vostro side.

**The stage 7 write already happens, inside stage 5's ACID transaction.** The flip to
`SETTLED` plus `clearing.settledAt` is at `contexts/payment_rail/execute.py`. Pulling it in
here would mean writing `payments` a second time, outside the atomic unit — so it stays
where it is until the lifecycle state machine can advance state without a bare `$set`
(plan §8 item 1).

That is why this file is a stub rather than a no-op with nothing behind it: the stage is
real and running, it just is not separable yet.

**Destiny: this context moves out.** Settlement against nostro/vostro accounts is a
different domain from payment initiation.

Reads  ctx: result (the settled payment)
Writes ctx: nothing today

TODO (Doina stage 7 Key Features):
  - real clearing-cycle modelling: a wire settles on a value date, not instantly. Today
    every payment settles synchronously, which is what makes the demo simple and also what
    makes settlement invisible.
  - nostro / vostro movement via InternalBankAccount — needs the chart-of-accounts
    extension (holding / mirror / clearing / nostro), which is deferred pending Doina or
    Payton (doc 10).
  - settlement confirmation from the rail (BIAN PaymentConfirmation, SD 47766), and the
    `settlementStatus` axis advancing independently of `status` (D1).
  - authorisation hold — `available` diverging from `current` between authorisation and
    settlement. Deferred to Phase 5 / cards (D5), and it will hurt then.
"""

from __future__ import annotations

from process.payment_context import PaymentContext


def run(ctx: PaymentContext) -> None:
    """No-op. The stage 7 write lives in stage 5's ACID block — see the module docstring."""
    return None
