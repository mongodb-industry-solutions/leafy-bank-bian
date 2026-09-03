"""Stage 8 — reconciliation.  [ASYNC — runs in the ledger service, not here]

BIAN AccountReconciliation (SD 35449, has a published semantic API).

**This stage moved out.** Reconciliation is a post-batch concern: leg 3 (*settlement
account ↔ GL*) needs the settlement `journalEntries` doc, which the GL batch produces minutes
after `settle.run` returns — so it cannot run synchronously in the saga. The work lives in the
**ledger service** (`reconciliation_service.reconcile_settled_payments`), invoked at the end of
`gl_batch.run_one_cycle`, exactly as stage 6's `POSTED` write-back lives in the ledger
(`posting_writeback_service`). This was the context's own predicted destiny (the prior
docstring read *"this context moves out, and probably merges with the ledger's existing
reconciliation service"*), and it honours Doina's L927 note that reconciliation is *"a described
capability of these domains, not a separate one."*

The three-way tie-out (payment ↔ rail, rail ↔ settlement, settlement ↔ GL) and the
`RECONCILED` transition are computed there. See `doc 22` and
`backend/ledger/services/reconciliation_service.py:compute_reconciliation`.

`run` is intentionally a no-op. It records one `checks[]` entry so stage 8 is visible in the
payment's check trail even though the actual reconciliation fires later, asynchronously, from
the batch.

Reads  ctx: current_state, payment_oid, collections
Writes ctx: one `checks[]` entry (SKIP); nothing on `payments` lifecycle
"""

from __future__ import annotations

from datetime import datetime, timezone

from contexts.payment_order_initiation.domain import checks
from process.payment_context import PaymentContext

STAGE = "8 reconcile"


def run(ctx: PaymentContext) -> None:
    """No-op in the saga. The ledger service reconciles post-batch (doc 22 B1).

    Records a single SKIP check so the stage appears in the checks panel — the RECONCILED
    transition itself is written by the ledger, asynchronously, and arrives as a
    `lifecycle.events` entry from `actor: ledger-service`.
    """
    checks.append_checks(
        ctx.collections.payments,
        ctx.payment_oid,
        [checks.check(
            STAGE, "reconciliation_deferred", checks.SKIP,
            mode=checks.ASYNC,
            detail=(
                "Three-way reconciliation runs in the ledger service after the GL batch posts "
                "the settlement journal (doc 22). RECONCILED arrives asynchronously."
            ),
            actor="transactions-service",
            at=datetime.now(timezone.utc),
        )],
    )
    return None
