"""Stage 4a — payment orchestration.  [NOT IMPLEMENTED]

BIAN PaymentOrchestration (SD 48782, no published semantic API — model it ourselves).

Because the customer already chose the rail in stage 1, orchestration decides **how** to
execute within that rail, not which rail to use (Doina stage 4A; decision D8). The logic is
nearly trivial today, which is why this is a stub rather than a context with layers — but
it owns two new collections, so the seam is real.

This is fill-in item 3 (plan §8) and the structural heart of Doina's model: `payments` is
what the customer *asked for*, `paymentOrders` is what the bank *committed to execute*.
**Hard to reverse** — collapsing the two later means re-splitting live data.

Reads  ctx: payment_doc, payment_rail, debtor_account, creditor_account, current_state
Writes ctx: current_state -> ROUTED; paymentOrder + routingSnapshot refs (once implemented)

`ROUTED` precedes `AUTHORISED` — you cannot fraud-score an unresolved destination (D1).
Doina's own sequence omits `ROUTED`; ours inserts it. Still on the "tell Doina" list.

TODO (plan §8 item 3):
  - `paymentOrders` — selected rail, clearing network, value date, confirmed
    amount/currency, FX, charges, execution conditions. A separate collection, not a
    sub-document (D4).
  - `routingSnapshots` — BIC/ABA, correspondent, SSI/settlement account, message standard,
    routing rationale, timestamp. **Written once, never updated.** Immutable routing
    evidence is the point.
  - expose `POST /PaymentOrchestration/Initiate` (D8).
"""

from __future__ import annotations

from contexts.payment_order_initiation.domain import lifecycle
from process.payment_context import PaymentContext


def run(ctx: PaymentContext) -> None:
    lifecycle.advance_ctx(
        ctx, lifecycle.ROUTED,
        actor="orchestration-service",
        reason=f"Execution path defaulted within {ctx.payment_rail} rail — no routing logic yet",
    )
