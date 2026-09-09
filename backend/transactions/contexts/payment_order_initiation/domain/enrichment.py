"""Stage 3 — payment enrichment and post-enrichment revalidation (BIAN PaymentOrderInitiation).

Doina stage 3, enrichment half (L441-L460). Doc 17 §3 steps 5 and 6.

Reads  ctx: payment_oid, payment_id, is_external_creditor, reference_data, collections
Writes ctx: current_state -> ENRICHED -> FINAL_VALIDATED; `payments.enrichment{}`;
            resolved fields on the document; `payments.checks[]` entries

## Two states, two jobs

`ENRICHED` — best-effort resolution. **Never refuses.** A directory miss is a WARN, because
one absent reference row must not fail every wire in the demo (doc 17 B7; the 2026-07-01
lesson about permanent vs transient failure, applied a layer up).

`FINAL_VALIDATED` — *"Post-enrichment revalidation"*, and this is the state's whole job.
**May refuse**, on exactly one thing: the spec's own `$expr` rule that a WIRE carries a BIC on
both agents. A missing routing number on a domestic wire WARNs instead — see `_final_validate`
for why that distinction is deliberate. Before this, both transitions fired unconditionally
and `FINAL_VALIDATED` was indistinguishable from `ENRICHED`.

The reason strings used to say *"No enrichment configured"* and *"nothing to revalidate"*.
They were honest then and would be false now, which is the Phase D failure mode this project
keeps finding in its own comments. They are generated from what actually happened.

## The before/after record

Every resolution appends `{field, from, to, source}` to `enrichment.resolved[]`, and the
pre-enrichment values of exactly those fields are snapshotted into `enrichment.original{}`
**before the first write**. Without it her L459-460 screen cannot be drawn at all: the
document holds one value per field, so enrichment destroys the "before". Doc 17 B1; doc 07's
P1 item 5.

`enrichment{}` is the **sixth** field written that the spec does not declare, after
`checks[]`, `authentication{}`, `entitlement{}` (stage 2) and `lifecycle{}`, `refs{}`
(D2/D3). Nullable, not `required`, added the same way. Doina ratifies it as Q21.
"""

from __future__ import annotations

from datetime import datetime, timezone

from contexts.payment_order_initiation.domain import (
    bank_identity,
    checks,
    enrichment_plan,
    lifecycle,
)
from process.payment_context import PaymentContext

STAGE = "3 enrich"
FINAL_STAGE = "3 final-validate"


def run(ctx: PaymentContext) -> None:
    now = datetime.now(timezone.utc)
    payments = ctx.collections.payments

    payment = payments.find_one({"_id": ctx.payment_oid}) or {}

    plan = enrichment_plan.plan(
        payment, ctx.reference_data, external_creditor=ctx.is_external_creditor,
        debtor_account_currency=(ctx.debtor_account or {}).get("currency"),
    )

    recorded = [
        checks.check(STAGE, name, result, mode=checks.SYNC, detail=detail, at=now)
        for name, result, detail in plan.outcomes
    ]

    # FR-3.12 — the regulatory-report plan is pure (no clock), so it leaves each report's
    # `assessedAt` null. Enrichment owns `now` (same as `resolvedAt`), so it stamps the
    # assessment time here, before the `$set`. Same discipline as `payment_document.build`
    # initialising a field null for a later stage to fill.
    for report in plan.updates.get("correspondent.regulatoryReports", []):
        report["assessedAt"] = now

    update: dict = dict(plan.updates)
    update["enrichment"] = {
        # Taken from the persisted document, before anything below is applied — which is
        # what makes it provably as-captured rather than a reconstruction.
        "original": enrichment_plan.snapshot_original(payment, list(plan.updates)),
        "resolved": plan.resolved,
        "resolvedAt": now,
        "actor": "transactions-service",
    }
    payments.update_one({"_id": ctx.payment_oid}, {"$set": update})
    checks.append_checks(payments, ctx.payment_oid, recorded)

    lifecycle.advance_ctx(
        ctx, lifecycle.ENRICHED,
        actor="transactions-service",
        reason=_enriched_reason(plan, payment),
    )

    _final_validate(ctx, now)


def _enriched_reason(plan, payment: dict) -> str:
    """What actually happened, in the words the lifecycle event will carry.

    For an intrabank transfer with nothing to resolve, this reproduces the spec's own sample
    wording — `internal_transfer`'s ENRICHED event reads *"No external enrichment required
    for an intrabank transfer"*.
    """
    if not plan.changed:
        if payment.get("rail") == "INTERNAL":
            return "No external enrichment required for an intrabank transfer"
        return "Enrichment resolved nothing — no reference data matched this payment"

    fields = ", ".join(sorted({entry["field"] for entry in plan.resolved}))
    return f"Enriched {len(plan.resolved)} field(s): {fields}"


def _final_validate(ctx: PaymentContext, now: datetime) -> None:
    """Step 6 — re-assert, after enrichment, what enrichment was meant to supply.

    Two rules, both scoped to WIRE, and **only the first refuses**:

    1. **Both BICs non-null — REFUSES.** This is the spec's own cross-field constraint,
       `validator.$and[1]`: `{"$cond": [{"$eq": ["$rail","WIRE"]}, {"$and": [{"$ne":
       ["$debtor.bic", null]}, {"$ne": ["$creditor.bic", null]}]}, true]}`. The collection
       validator is not applied (Doina Q9 — `fraud` is required + non-nullable and blocks
       it), so nothing else enforces it. A test asserts this rule matches the spec file
       rather than trusting the transcription.
    2. **A routing number on both agents, for a DOMESTIC wire — WARNS.** D10's rationale
       stands (the sample is unsendable without one), but the spec has **no `$expr` for it**
       — a gap this project raised as Q23.

    ⚠️ **Why 2 warns, corrected while building (2026-08-31).** It was written as a refusal
    and that was wrong: a routing number is missing precisely *because* the beneficiary bank
    was not in the directory, so refusing here would make a thin directory fail every
    external wire — the exact outcome B7 exists to prevent, arriving one step later than the
    WARN that was supposed to absorb it. The rule that refuses is the one the **spec**
    states; the rule we merely believe warns. Sending an unaddressable wire is stage 5's
    problem, and stage 5 still halts external beneficiaries anyway.

    Refuses via `ValueError`, like `validation.run`: the saga marks the payment REJECTED and
    the trail shows `VALIDATED -> ENRICHED -> REJECTED`, which is exactly the diagnosis a
    reader wants — enrichment ran and did not produce what execution needs.
    """
    payments = ctx.collections.payments
    payment = payments.find_one({"_id": ctx.payment_oid}) or {}
    recorded: list = []

    def record(name: str, result: str, detail: str) -> None:
        recorded.append(
            checks.check(FINAL_STAGE, name, result, mode=checks.SYNC, detail=detail, at=now)
        )

    def refuse(name: str, detail: str) -> None:
        record(name, checks.FAIL, detail)
        checks.append_checks(payments, ctx.payment_oid, recorded)
        raise ValueError(detail)

    if payment.get("rail") != "WIRE":
        record(
            "wire_agents_complete", checks.SKIP,
            f"Rail {payment.get('rail')} carries no interbank agent requirements.",
        )
    else:
        debtor = payment.get("debtor") or {}
        creditor = payment.get("creditor") or {}

        missing = [
            side for side, party in (("debtor", debtor), ("creditor", creditor))
            if not party.get("bic")
        ]
        if missing:
            refuse(
                "wire_agents_complete",
                f"A WIRE requires a BIC on both agents; {' and '.join(missing)} has none "
                "after enrichment. The beneficiary bank could not be resolved.",
            )

        domestic = (
            debtor.get("bankCountry") == bank_identity.OUR_BANK_COUNTRY
            and creditor.get("bankCountry") == bank_identity.OUR_BANK_COUNTRY
        )
        no_routing = [
            side for side, party in (("debtor", debtor), ("creditor", creditor))
            if not party.get("clearingSystemMemberId")
        ] if domestic else []

        if no_routing:
            record(
                "wire_agents_complete", checks.WARN,
                f"A domestic wire is addressed by routing number and "
                f"{' and '.join(no_routing)} has none after enrichment (D10) — the "
                "beneficiary bank is not in the institution directory. Not refused: the "
                "spec constrains only the BICs, and a thin directory is our gap. Stage 5 "
                "cannot address this wire as it stands.",
            )
            checks.append_checks(payments, ctx.payment_oid, recorded)
            lifecycle.advance_ctx(
                ctx, lifecycle.FINAL_VALIDATED,
                actor="transactions-service",
                reason="Post-enrichment revalidation passed with warnings",
            )
            return

        record(
            "wire_agents_complete", checks.PASS,
            f"{debtor.get('bic')} -> {creditor.get('bic')}"
            + (f", routing {debtor.get('clearingSystemMemberId')} -> "
               f"{creditor.get('clearingSystemMemberId')}" if domestic else "")
            + f" — {'DOMESTIC' if domestic else 'INTERNATIONAL'} wire, agents complete.",
        )

    checks.append_checks(payments, ctx.payment_oid, recorded)

    lifecycle.advance_ctx(
        ctx, lifecycle.FINAL_VALIDATED,
        actor="transactions-service",
        reason="Post-enrichment revalidation passed",
    )
