"""Stage 8 — reconciliation.  [NOT IMPLEMENTED]

BIAN AccountReconciliation (SD 35449, has a published semantic API).

Nothing runs in the payment path. Note that reconciliation **does** already exist
downstream: `backend/ledger/services/reconciliation_service.py` runs a pre-batch gate
inside the GL pipeline. So this stage has a home to move to rather than a home to build.

**Destiny: this context moves out**, and probably merges with the ledger's existing
reconciliation service rather than becoming a new one.

Reads  ctx: result
Writes ctx: nothing today

TODO (Doina stage 8 Key Features):
  - reconcile the rail's settlement report against `paymentExecutions` — which needs
    paymentExecutions first (plan §8 item 4).
  - three-way tie-out: payment instruction vs. internal movement vs. external
    confirmation. Breaks feed stage 9.
  - the `RECONCILED` lifecycle state (D1) is currently unreachable; nothing advances a
    payment past `SETTLED`.
  - a reconciliation check that walks back to the ultimate source collection, not just
    mid-pipeline ones — a fully-dropped record must stay detectable after the fact
    (prevention rule, defects.md 2026-07-01).
"""

from __future__ import annotations

from process.payment_context import PaymentContext


def run(ctx: PaymentContext) -> None:
    """No-op. See the module docstring for what belongs here."""
    return None
