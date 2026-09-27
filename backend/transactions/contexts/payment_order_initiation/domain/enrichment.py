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
from typing import Optional

from contexts.payment_order_initiation.domain import (
    bank_identity,
    checks,
    enrichment_plan,
    lifecycle,
)
from process.payment_context import PaymentContext
from contexts.payment_order_initiation.ports.enrichment_agent import NullEnrichmentAgent

STAGE = "3 enrich"
FINAL_STAGE = "3 final-validate"

# The only fields the Enrichment Agent may propose. Everything else on the payment is either
# deterministic-planner territory or mandatory customer input the agent must not touch
# (spec L679). Mirrors the agent service's own allowlist. The purpose-code pair is semantic
# classification; the remittance-reference pair is NLP extraction from free text — both target
# spec-declared nullable fields the deterministic planner does not write.
_AGENT_ALLOWED_FIELDS = frozenset({
    "remittance.purposeCode",
    "categoryPurpose",
    "remittance.reference",
    "remittance.invoiceNo",
    "creditor.bic",
    "creditor.bankName",
    "creditor.bankCountry",
    "creditor.clearingSystemMemberId",
    "creditor.clearingSystemCode",
})

# The five beneficiary-bank fields the agent may propose. The agent's proposal is treated as
# "use BIC X"; the transactions-side directory port re-validates and supplies canonical values.
_AGENT_BANK_FIELDS = frozenset({
    "creditor.bic",
    "creditor.bankName",
    "creditor.bankCountry",
    "creditor.clearingSystemMemberId",
    "creditor.clearingSystemCode",
})


def run(ctx: PaymentContext) -> None:
    now = datetime.now(timezone.utc)
    payments = ctx.collections.payments

    payment = payments.find_one({"_id": ctx.payment_oid}) or {}

    # When the Enrichment Agent is configured, it owns external-creditor bank enrichment
    # (Doina Sep 17 L665-689): skip the deterministic external lookup so the agent is the
    # genuine owner, not a decorative re-tagger. The internal-creditor bank_identity stamping
    # always runs (our own identity, not agent territory). If the agent then produces no
    # usable bank proposal, `plan_external_creditor_bank` runs as the fallback below — so the
    # money path never depends on the agent.
    agent_configured = not isinstance(ctx.enrichment_agent, NullEnrichmentAgent)
    plan = enrichment_plan.plan(
        payment, ctx.reference_data, external_creditor=ctx.is_external_creditor,
        debtor_account_currency=(ctx.debtor_account or {}).get("currency"),
        skip_external_creditor_bank=agent_configured,
    )

    # Phase-1 Enrichment Agent (option B): ask the agent for beneficiary-bank enrichment
    # (from the BIC via the directory), a semantic purpose-code proposal, AND any structured
    # remittance references it can extract from free text, then fold accepted proposals into
    # the same plan the deterministic planner produced. Best-effort — `[]` when the agent is
    # unconfigured or down (NullEnrichmentAgent / HTTP failure), so the saga proceeds exactly
    # as before. The agent never writes the payment; this `$set` is the only writer. Bank and
    # purpose-code proposals are re-validated against the directory/table (FR-3.11) before
    # recording; extraction proposals are free strings with no table. A customer-supplied
    # value always wins. Returns an `enrichment.agent{}` block (or None) for the UI callout —
    # the agent's reasoning, confidence, and candidate trace, mirroring the Reconciliation
    # Agent's `exceptions.agent{}`.
    agent_proposals = ctx.enrichment_agent.propose(ctx.payment_id, payment)
    agent_block = _apply_agent_proposals(
        plan, payment, ctx.reference_data, agent_proposals, now,
        external_creditor=ctx.is_external_creditor,
    )

    # Deterministic fallback: the agent was configured but produced no usable bank proposal
    # (empty proposals, no creditor.* fields, or BIC failed re-validation). Run the external
    # branch now so the payment still gets bank enrichment. When the agent is NOT configured,
    # the external branch already ran inside `plan()` above.
    if agent_configured and not any(r["field"] in _AGENT_BANK_FIELDS for r in plan.resolved):
        enrichment_plan.plan_external_creditor_bank(
            plan, payment, ctx.reference_data,
            external_creditor=ctx.is_external_creditor, rail=payment.get("rail"),
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

    # FR-3.14 — the `fx{}` object is planned pure (rateTimestamp/quoteId null); stamp both
    # from `now` here, same discipline. quoteId is deterministic-per-payment so a re-run
    # restates the same quote rather than minting a new one.
    fx = plan.updates.get("fx")
    if isinstance(fx, dict):
        fx["rateTimestamp"] = now
        fx["quoteId"] = f"FXQ-{now:%Y%m%d}-{payment.get('paymentId', 'unknown')[-12:]}"

    update: dict = dict(plan.updates)
    enrichment_doc = {
        # Taken from the persisted document, before anything below is applied — which is
        # what makes it provably as-captured rather than a reconstruction.
        "original": enrichment_plan.snapshot_original(payment, list(plan.updates)),
        "resolved": plan.resolved,
        "resolvedAt": now,
        "actor": "transactions-service",
    }
    # The agent's reasoning block — present only when the agent proposed something that was
    # applied. Read by the Stage-3 "AI enrichment" callout in the deep dive. Nested under the
    # already-undeclared `enrichment` field (Q21), so it adds no new top-level key.
    if agent_block is not None:
        enrichment_doc["agent"] = agent_block
    update["enrichment"] = enrichment_doc
    payments.update_one({"_id": ctx.payment_oid}, {"$set": update})
    checks.append_checks(payments, ctx.payment_oid, recorded)

    lifecycle.advance_ctx(
        ctx, lifecycle.ENRICHED,
        actor="transactions-service",
        reason=_enriched_reason(plan, payment),
    )

    _final_validate(ctx, now)


def _apply_agent_proposals(
    plan, payment: dict, reference_data, proposals, now, *, external_creditor: bool,
) -> Optional[dict]:
    """Fold accepted agent proposals into the deterministic plan (option B).

    Three proposal kinds, all best-effort and customer-supplied-wins:

    - **Beneficiary bank** (`creditor.bic` / `bankName` / `bankCountry` /
      `clearingSystemMemberId` / `clearingSystemCode`): the agent resolves the bank from the
      customer-supplied BIC via the institution directory. Re-validated through
      `reference_data.bank_by_bic` (FR-3.11 analogue): the agent proposes, the deterministic
      directory gates, and the directory's **canonical** values are applied — never the
      agent's restated strings — so a typo'd bankName cannot land. A BIC not in the directory
      is a WARN and applies nothing (the caller's fallback then runs).
    - **Purpose code** (`remittance.purposeCode` / `categoryPurpose`): the first valid
      proposal is mirrored onto both fields, but only where the customer did not supply a
      code. Re-validated against the `purposeCodes` table (FR-3.11): the agent proposes, the
      deterministic table gates.
    - **Remittance references** (`remittance.reference` / `remittance.invoiceNo`): extracted
      from the customer's free-text `remittance.unstructured`. No table re-validation (free
      strings, no enum); a customer-supplied value wins. The agent must not invent a
      reference not present in the free text — that is enforced upstream by the prompt and
      the allowlist, not here.

    Every applied resolution is tagged `source: "agent"` with its `reason` + `confidence` so
    the before/after panel distinguishes AI-proposed values and can show why. Returns an
    `enrichment.agent{}` block (confidence, the remittance text, the applied proposals with
    their candidate trace, and a timestamp) for the UI callout, or None when nothing applied.

    Never raises: a malformed proposal or a table miss is a WARN outcome, not a failure.
    """
    applied: list[dict] = []  # the records that populate the UI callout

    # --- purpose code -----------------------------------------------------------
    # Gate on whether the CUSTOMER supplied a code, not on `plan.updates` membership.
    # The planner mirrors a single supplied code to the other field (`_plan_purpose_codes`),
    # so `plan.updates` holds the mirror but not the original — a membership check would
    # let the agent overwrite the customer's original code. "Customer-supplied code wins"
    # (doc 17 B7) must hold for the single-field case too.
    category_supplied = payment.get("categoryPurpose") is not None
    remittance_supplied = (payment.get("remittance") or {}).get("purposeCode") is not None
    if not (category_supplied or remittance_supplied):
        for prop in proposals or []:
            field = prop.get("field") if isinstance(prop, dict) else None
            if field not in ("remittance.purposeCode", "categoryPurpose"):
                continue
            code = prop.get("to")
            if not code:
                continue
            record = reference_data.purpose_code(code)
            if record is None:
                plan.outcomes.append((
                    "purpose_code_resolved", "WARN",
                    f"Enrichment Agent proposed {code!r}, which is not in the reference table — "
                    "not applied. Carried as supplied (none).",
                ))
                continue
            reason = str(prop.get("reason", ""))
            confidence = str(prop.get("confidence", "")).upper() or None
            if not category_supplied:
                plan._set(
                    "categoryPurpose", record.code,
                    before=payment.get("categoryPurpose"), source="agent",
                    reason=reason, confidence=confidence,
                )
                category_supplied = True
            if not remittance_supplied:
                plan._set(
                    "remittance.purposeCode", record.code,
                    before=(payment.get("remittance") or {}).get("purposeCode"),
                    source="agent", reason=reason, confidence=confidence,
                )
                remittance_supplied = True
            # The planner recorded a SKIP ("semantic matching deferred") for this payment
            # before the agent was consulted. Now that the agent HAS proposed a code, that
            # SKIP is stale and would contradict the PASS below in the checks trail. Drop it.
            plan.outcomes = [
                o for o in plan.outcomes
                if not (o[0] == "purpose_code_resolved" and o[1] == "SKIP")
            ]
            plan.outcomes.append((
                "purpose_code_resolved", "PASS",
                f"{record.code} — {record.name} (agent-proposed, validated against the "
                "purpose-code table). Feeds AML scoring and routing priority in stage 4.",
            ))
            applied.append({
                "field": "remittance.purposeCode", "to": record.code,
                "reason": reason, "confidence": confidence,
                "considered": prop.get("considered") or [],
            })
            break  # one code per payment; both fields are now set from it.

    # --- remittance references (NLP extraction) ---------------------------------
    remittance = payment.get("remittance") or {}
    for prop in proposals or []:
        field = prop.get("field") if isinstance(prop, dict) else None
        if field not in ("remittance.reference", "remittance.invoiceNo"):
            continue
        leaf = field.split(".")[-1]
        if remittance.get(leaf) is not None:
            # Customer supplied this reference; the agent has nothing to add.
            continue
        value = prop.get("to")
        if not value:
            continue
        reason = str(prop.get("reason", ""))
        confidence = str(prop.get("confidence", "")).upper() or None
        plan._set(
            field, value,
            before=remittance.get(leaf), source="agent",
            reason=reason, confidence=confidence,
        )
        plan.outcomes.append((
            "remittance_reference_resolved", "PASS",
            f"{leaf} extracted from remittance free text by the Enrichment Agent: {value}.",
        ))
        applied.append({
            "field": field, "to": value,
            "reason": reason, "confidence": confidence,
        })

    # --- beneficiary bank (reference-data association) --------------------------
    # External creditors only. For an internal creditor the bank is us, and the deterministic
    # `bank_identity` stamping in `plan()` already owns `creditor.clearingSystem*` — letting
    # the agent also write bank fields here would double-write the same field (bank_identity
    # row + agent row), because the customer-supplied-wins gate reads the original doc, not
    # the planner's pending updates. Bank enrichment is external-creditor-only by design
    # (Doina's example is an external BIC → beneficiary bank).
    #
    # The agent resolves the bank FROM the customer-supplied BIC. Treat the agent's
    # `creditor.bic` proposal as the lookup key if present, else fall back to the BIC already
    # on the payment (the common case — the customer supplied the BIC, the agent fills the
    # rest). Re-validate through the transactions-side directory port and apply the record's
    # CANONICAL values, never the agent's restated strings. Customer-supplied bank fields
    # win per-field. A BIC the directory doesn't know is a WARN → the caller's fallback runs.
    bank_props = [p for p in (proposals or [])
                  if isinstance(p, dict) and p.get("field") in _AGENT_BANK_FIELDS]
    if bank_props and external_creditor:
        creditor = payment.get("creditor") or {}
        bic_prop = next((p for p in bank_props if p.get("field") == "creditor.bic"), None)
        lookup_bic = (bic_prop.get("to") if bic_prop else None) or creditor.get("bic")
        record = reference_data.bank_by_bic(lookup_bic) if lookup_bic else None
        if record is None:
            plan.outcomes.append((
                "beneficiary_bank_resolved", "WARN",
                f"Enrichment Agent proposed BIC {lookup_bic!r}, which is not in the "
                "institution directory — not applied. The deterministic fallback will run.",
            ))
        else:
            canonical = {
                "creditor.bic": record.bic,
                "creditor.bankName": record.bank_name,
                "creditor.bankCountry": record.bank_country,
                "creditor.clearingSystemMemberId": record.clearing_system_member_id,
                "creditor.clearingSystemCode": record.clearing_system_code,
            }
            # Carry the reason/confidence from any bank proposal (they share a lookup).
            reason = str(bank_props[0].get("reason", ""))
            confidence = str(bank_props[0].get("confidence", "")).upper() or None
            for prop in bank_props:
                field = prop.get("field")
                if creditor.get(field.split(".")[-1]) is not None:
                    # Customer supplied this bank field; the agent has nothing to add.
                    continue
                value = canonical.get(field)
                if value is None:
                    continue
                plan._set(
                    field, value,
                    before=creditor.get(field.split(".")[-1]), source="agent",
                    reason=reason, confidence=confidence,
                )
                applied.append({
                    "field": field, "to": value,
                    "reason": reason, "confidence": confidence,
                    "considered": prop.get("considered") or [],
                })
            # Drop a stale deterministic SKIP/WARN for the bank if the agent resolved it, and
            # record a PASS. (The planner did not run the external branch when the agent is
            # configured, so there is usually nothing to drop — but be defensive.)
            plan.outcomes = [
                o for o in plan.outcomes
                if not (o[0] == "beneficiary_bank_resolved" and o[1] in ("SKIP", "WARN"))
            ]
            plan.outcomes.append((
                "beneficiary_bank_resolved", "PASS",
                f"{record.bic} resolved to {record.bank_name} ({record.bank_country}) "
                "by the Enrichment Agent (validated against the institution directory).",
            ))

    if not applied:
        return None

    # Highest confidence among the applied proposals (HIGH > MEDIUM > LOW > unknown).
    rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    confidence = max(
        (a.get("confidence") for a in applied), key=lambda c: rank.get(c or "", 0), default=None,
    ) or None
    return {
        "confidence": confidence,
        "remittanceText": remittance.get("unstructured"),
        "proposals": applied,
        "recordedAt": now.isoformat(),
    }


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

    def record(name: str, result: str, detail: str, *,
               field: str | None = None, code: str | None = None) -> None:
        recorded.append(
            checks.check(FINAL_STAGE, name, result, mode=checks.SYNC, detail=detail,
                         field=field, code=code, at=now)
        )

    def refuse(name: str, detail: str, *, field: str | None = None,
               code: str | None = None) -> None:
        record(name, checks.FAIL, detail, field=field, code=code)
        checks.stamp_validation_summary(payments, ctx.payment_oid, recorded)
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
                field=", ".join(f"{s}.bic" for s in missing),
                code="WIRE_BIC_MISSING",
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
            checks.stamp_validation_summary(payments, ctx.payment_oid, recorded)
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

    checks.stamp_validation_summary(payments, ctx.payment_oid, recorded)
    checks.append_checks(payments, ctx.payment_oid, recorded)

    lifecycle.advance_ctx(
        ctx, lifecycle.FINAL_VALIDATED,
        actor="transactions-service",
        reason="Post-enrichment revalidation passed",
    )
