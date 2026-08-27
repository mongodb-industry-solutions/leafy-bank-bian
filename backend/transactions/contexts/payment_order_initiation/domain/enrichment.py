"""Stage 3 — payment enrichment (BIAN PaymentOrderInitiation, SD 42933).  [NOT IMPLEMENTED]

Doina stage 3, enrichment half. No code today: the payment document is built with
`remittance.purposeCode: None`, `correspondent.sanctionsCheck` hardcoded to CLEAR, and no
correspondent-bank resolution.

This is fill-in item 2 (plan §8) and the first stage to need a **port**. Enrichment reads
reference data (purpose codes, BIC directory, correspondent banks) which lives in Mongo —
but this module must stay pure and take a `ReferenceData` port so it is unit-testable with
no Atlas connection. Stage 3 grows from six fields to sixty; that only stays testable if
the dependency points inward. Create `ports/reference_data.py` and
`adapters/mongo_reference_data.py` when this lands, not before.

Reads  ctx: debtor_account, creditor_account, payment_rail  (once implemented)
Writes ctx: current_state -> ENRICHED -> FINAL_VALIDATED, payment_doc

Both transitions fire today even though no field is enriched. That is deliberate: the states
are what the demo renders, and a stage that silently skips its own transition is invisible.
The `reason` strings say plainly that nothing was resolved.

TODO (Doina stage 3 Key Features):
  - correspondent bank resolution via BIAN CorrespondentBankDirectory (SD 39320) and
    CorrespondentBankRelationshipManagement (SD 34643).
  - purpose / category purpose code lookup against the reference table.
  - sanctions screening — currently hardcoded CLEAR at `persist.py`.
  - clearingSystemMemberId / clearingSystemCode on both agents (D10). The schema has the
    fields as of 2026-08-27; nothing populates them, and the `wire_domestic` sample is
    unsendable without them.
"""

from __future__ import annotations

from contexts.payment_order_initiation.domain import lifecycle
from process.payment_context import PaymentContext


def run(ctx: PaymentContext) -> None:
    lifecycle.advance_ctx(
        ctx, lifecycle.ENRICHED,
        actor="transactions-service",
        reason="No enrichment configured — routing data and purpose code not resolved",
    )
    lifecycle.advance_ctx(
        ctx, lifecycle.FINAL_VALIDATED,
        actor="transactions-service",
        reason="Post-enrichment revalidation passed (nothing to revalidate)",
    )
