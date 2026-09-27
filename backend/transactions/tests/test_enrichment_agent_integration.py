"""Tests for the phase-1 Enrichment Agent integration into stage 3 (option B).

Pins the money-path safety contract:
- the agent's proposal is applied ONLY for purpose-code fields the deterministic planner did
  not already resolve (a customer-supplied code wins);
- every proposal is re-validated against the `purposeCodes` table (FR-3.11) — a code not in
  the table is a WARN, not an applied value;
- disallowed fields are dropped;
- empty/null proposals change nothing.

`_apply_agent_proposals` is tested directly with hand-built `EnrichmentPlan` instances so the
planner's other resolution paths don't muddy the assertion. The full `enrichment.run` path is
already covered by `test_stage_three_enrichment.py` (which uses `NullEnrichmentAgent` via the
context default, so it exercises the no-agent degradation).
"""

from __future__ import annotations

from datetime import datetime, timezone

from contexts.payment_order_initiation.domain import enrichment, enrichment_plan
from contexts.payment_order_initiation.ports.reference_data import (
    BankRecord,
    InMemoryReferenceData,
    PurposeCodeRecord,
)


def _now():
    return datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def _ref():
    return InMemoryReferenceData(
        purpose_codes=[
            PurposeCodeRecord(code="SUPP", name="Supplier Payment",
                              description="desc", category="TRADE"),
            PurposeCodeRecord(code="SALA", name="Salary Payment",
                              description="desc", category="PAYROLL"),
        ]
    )


_BARC = BankRecord(
    bic="BARCGB22", bank_name="Barclays Bank PLC", bank_country="GB",
    clearing_system_code="GBDSC", clearing_system_member_id="ABC123", city="London",
)


def _ref_with_banks():
    return InMemoryReferenceData(
        banks=[_BARC],
        purpose_codes=[
            PurposeCodeRecord(code="SUPP", name="Supplier Payment",
                              description="desc", category="TRADE"),
        ],
    )


def test_agent_proposal_applied_when_planner_skipped():
    payment = {"rail": "WIRE", "remittance": {"unstructured": "supplier invoice"}}
    plan = enrichment_plan.EnrichmentPlan()  # planner resolved nothing
    enrichment._apply_agent_proposals(
        plan, payment, _ref(),
        [{"field": "remittance.purposeCode", "to": "SUPP", "reason": "r", "source": "agent"}],
        _now(), external_creditor=False,
    )
    assert plan.updates["categoryPurpose"] == "SUPP"
    assert plan.updates["remittance.purposeCode"] == "SUPP"
    # Both resolutions tagged agent, with the pre-value snapshotted from the payment.
    agent_rows = [r for r in plan.resolved if r["source"] == "agent"]
    assert {r["field"] for r in agent_rows} == {"categoryPurpose", "remittance.purposeCode"}
    assert all(r["from"] is None for r in agent_rows)


def test_customer_supplied_code_wins_over_agent():
    # Simulate the deterministic planner having already resolved the purpose code.
    payment = {"rail": "WIRE", "categoryPurpose": "SALA",
               "remittance": {"purposeCode": "SALA"}}
    plan = enrichment_plan.EnrichmentPlan()
    plan.updates["categoryPurpose"] = "SALA"
    plan.updates["remittance.purposeCode"] = "SALA"
    plan.resolved.append({"field": "categoryPurpose", "from": None, "to": "SALA",
                          "source": "purposeCodes"})
    enrichment._apply_agent_proposals(
        plan, payment, _ref(),
        [{"field": "remittance.purposeCode", "to": "SUPP", "reason": "r", "source": "agent"}],
        _now(), external_creditor=False,
    )
    # Agent must not overwrite the planner's resolution.
    assert plan.updates["categoryPurpose"] == "SALA"
    assert plan.updates["remittance.purposeCode"] == "SALA"
    assert not any(r["source"] == "agent" for r in plan.resolved)


def test_customer_supplied_single_field_code_wins_over_agent():
    # Customer supplied a valid code on ONE field only; the planner mirrors it to the other.
    # The agent must not overwrite either — "customer-supplied code wins" holds for the mirror
    # too. Regression for the C-2 gap: the old plan.updates-membership guard let the agent
    # overwrite the customer's original code because the mirror put the other field in updates.
    payment = {"rail": "WIRE", "categoryPurpose": "GDDS", "remittance": {}}
    plan = enrichment_plan.EnrichmentPlan()
    # Planner validated GDDS and mirrored it to remittance.purposeCode.
    plan.updates["remittance.purposeCode"] = "GDDS"
    plan.resolved.append({"field": "remittance.purposeCode", "from": None, "to": "GDDS",
                          "source": "purposeCodes"})
    enrichment._apply_agent_proposals(
        plan, payment, _ref(),
        [{"field": "categoryPurpose", "to": "SUPP", "reason": "r", "source": "agent"}],
        _now(), external_creditor=False,
    )
    # Customer's GDDS on categoryPurpose is untouched (agent did not overwrite it).
    assert plan.updates.get("categoryPurpose") is None
    assert payment.get("categoryPurpose") == "GDDS"
    # ...and the planner's mirror on remittance.purposeCode is untouched.
    assert plan.updates["remittance.purposeCode"] == "GDDS"
    assert not any(r["source"] == "agent" for r in plan.resolved)


def test_agent_proposal_not_in_table_is_warned_not_applied():
    payment = {"rail": "WIRE", "remittance": {}}
    plan = enrichment_plan.EnrichmentPlan()
    enrichment._apply_agent_proposals(
        plan, payment, _ref(),
        [{"field": "remittance.purposeCode", "to": "BOGUS", "source": "agent"}],
        _now(), external_creditor=False,
    )
    assert "categoryPurpose" not in plan.updates
    assert "remittance.purposeCode" not in plan.updates
    assert any(o[0] == "purpose_code_resolved" and o[1] == "WARN" for o in plan.outcomes)


def test_disallowed_field_dropped():
    payment = {"rail": "WIRE", "remittance": {}, "creditor": {"bic": "OLDBIC"}}
    plan = enrichment_plan.EnrichmentPlan()
    enrichment._apply_agent_proposals(
        plan, payment, _ref(),
        [{"field": "debtor.bic", "to": "BARCGB22", "source": "agent"}],
        _now(), external_creditor=False,
    )
    assert "debtor.bic" not in plan.updates


def test_empty_proposals_change_nothing():
    payment = {"rail": "WIRE", "remittance": {}}
    plan = enrichment_plan.EnrichmentPlan()
    enrichment._apply_agent_proposals(plan, payment, _ref(), [], _now(), external_creditor=False)
    assert plan.updates == {}
    assert plan.resolved == []


def test_null_enrichment_agent_via_context():
    """The default NullEnrichmentAgent on PaymentContext proposes nothing — the saga runs
    deterministically without an agent service (the pre-phase-1 behaviour)."""
    from process.payment_context import PaymentContext, PaymentCollections
    ctx = PaymentContext(
        customer_ref="CUST-1", debtor_account_ref="ACC-1",
        instructed_amount=100.0, instructed_currency="USD",
        payment_type="CREDIT_TRANSFER", payment_rail="WIRE",
        collections=PaymentCollections(db=None, customers=None, accounts=None,
                                       payments=None, transactions=None,
                                       notifications=None),
    )
    assert ctx.enrichment_agent.propose("PAY-1", {}) == []


# --- remittance-reference extraction (the broadened agent job) ---------------

def test_agent_extracts_invoice_no_from_free_text():
    payment = {"rail": "WIRE", "remittance": {"unstructured": "Payment for invoice INV-48392"}}
    plan = enrichment_plan.EnrichmentPlan()
    block = enrichment._apply_agent_proposals(
        plan, payment, _ref(),
        [{"field": "remittance.invoiceNo", "to": "INV-48392", "reason": "extracted",
          "confidence": "HIGH", "source": "agent"}],
        _now(), external_creditor=False,
    )
    assert plan.updates["remittance.invoiceNo"] == "INV-48392"
    row = next(r for r in plan.resolved if r["field"] == "remittance.invoiceNo")
    assert row["source"] == "agent"
    assert row["reason"] == "extracted"
    assert row["confidence"] == "HIGH"
    assert block is not None
    assert block["proposals"][0]["to"] == "INV-48392"


def test_customer_supplied_reference_wins_over_agent():
    payment = {"rail": "WIRE", "remittance": {"unstructured": "see INV-1",
               "invoiceNo": "CUST-INV"}}
    plan = enrichment_plan.EnrichmentPlan()
    enrichment._apply_agent_proposals(
        plan, payment, _ref(),
        [{"field": "remittance.invoiceNo", "to": "INV-1", "reason": "extracted",
          "source": "agent"}],
        _now(), external_creditor=False,
    )
    # The customer's invoiceNo is untouched; the agent did not overwrite it.
    assert "remittance.invoiceNo" not in plan.updates
    assert not any(r["field"] == "remittance.invoiceNo" for r in plan.resolved)


def test_agent_block_carries_confidence_and_remittance_text():
    payment = {"rail": "WIRE", "remittance": {"unstructured": "supplier invoice INV-9"}}
    plan = enrichment_plan.EnrichmentPlan()
    block = enrichment._apply_agent_proposals(
        plan, payment, _ref(),
        [
            {"field": "remittance.purposeCode", "to": "SUPP", "reason": "supplier",
             "confidence": "HIGH", "source": "agent",
             "considered": [{"code": "SUPP", "name": "Supplier", "score": 0.9}]},
            {"field": "remittance.invoiceNo", "to": "INV-9", "reason": "extracted",
             "confidence": "MEDIUM", "source": "agent"},
        ],
        _now(), external_creditor=False,
    )
    assert block["remittanceText"] == "supplier invoice INV-9"
    # Highest confidence among applied proposals wins.
    assert block["confidence"] == "HIGH"
    fields = {p["field"] for p in block["proposals"]}
    assert fields == {"remittance.purposeCode", "remittance.invoiceNo"}
    # The candidate trace rides on the purpose-code proposal only.
    pc = next(p for p in block["proposals"] if p["field"] == "remittance.purposeCode")
    assert pc["considered"][0]["code"] == "SUPP"
    inv = next(p for p in block["proposals"] if p["field"] == "remittance.invoiceNo")
    assert inv.get("considered", []) == []


def test_agent_block_none_when_nothing_applied():
    payment = {"rail": "WIRE", "remittance": {}}
    plan = enrichment_plan.EnrichmentPlan()
    block = enrichment._apply_agent_proposals(plan, payment, _ref(), [], _now(), external_creditor=False)
    assert block is None


# --- producer/consumer: the agent's output actually survives the gate --------
# Defect class 2026-08-31: a consumer's requirement must be satisfiable by the real producer.
# Here the "producer" is the agent's proposal wire shape; the "consumer" is the apply gate +
# the spec. Every field the agent can emit must (a) be in the allowlist and (b) be a
# spec-declared payment field, or the proposal is silently dropped and the agent's work is
# invisible — the exact failure this change fixes.

def test_every_agent_proposal_field_is_in_the_allowlist():
    # The three proposal kinds the agent service is instructed to emit (prompt + allowlist):
    # beneficiary-bank (5 fields), purpose code (2), remittance references (2).
    agent_emitted_fields = {
        "remittance.purposeCode", "categoryPurpose",
        "remittance.reference", "remittance.invoiceNo",
        "creditor.bic", "creditor.bankName", "creditor.bankCountry",
        "creditor.clearingSystemMemberId", "creditor.clearingSystemCode",
    }
    assert agent_emitted_fields <= enrichment._AGENT_ALLOWED_FIELDS


def test_deterministic_resolutions_still_have_exactly_four_keys():
    """The _set extension adds reason/confidence only when non-None. Deterministic callers
    pass neither, so their resolved entries stay on the original {field,from,to,source} shape
    — a deterministic entry must NOT carry the agent-only keys."""
    plan = enrichment_plan.EnrichmentPlan()
    plan._set("creditor.bankName", "Foo Bank", before=None, source="correspondentBanks")
    entry = plan.resolved[0]
    assert set(entry) == {"field", "from", "to", "source"}


# --- beneficiary-bank enrichment (Doina's flagship agent job) -----------------

def test_agent_resolves_beneficiary_bank_from_bic():
    """The agent proposes bank fields resolved from the customer's BIC. The transactions
    side re-validates via the directory and applies CANONICAL values, tagged source=agent."""
    payment = {"rail": "WIRE", "creditor": {"bic": "BARCGB22"},
               "remittance": {"unstructured": "supplier payment"}}
    plan = enrichment_plan.EnrichmentPlan()
    block = enrichment._apply_agent_proposals(
        plan, payment, _ref_with_banks(),
        [
            {"field": "creditor.bankName", "to": "Barclays Bank PLC", "reason": "from BIC",
             "confidence": "HIGH", "source": "agent"},
            {"field": "creditor.bankCountry", "to": "GB", "reason": "from BIC",
             "confidence": "HIGH", "source": "agent"},
            {"field": "creditor.clearingSystemMemberId", "to": "ABC123", "reason": "from BIC",
             "confidence": "HIGH", "source": "agent"},
        ],
        _now(), external_creditor=True,
    )
    # Canonical directory values applied, tagged agent.
    assert plan.updates["creditor.bankName"] == "Barclays Bank PLC"
    assert plan.updates["creditor.bankCountry"] == "GB"
    assert plan.updates["creditor.clearingSystemMemberId"] == "ABC123"
    bank_rows = [r for r in plan.resolved if r["field"].startswith("creditor.")]
    assert all(r["source"] == "agent" for r in bank_rows)
    assert all(r["confidence"] == "HIGH" for r in bank_rows)
    # The agent block lists the bank proposals.
    assert block is not None
    block_fields = {p["field"] for p in block["proposals"]}
    assert "creditor.bankName" in block_fields
    # A PASS outcome was recorded for the bank.
    assert any(o[0] == "beneficiary_bank_resolved" and o[1] == "PASS" for o in plan.outcomes)


def test_agent_bank_proposal_applies_canonical_not_restated():
    """Even if the agent restates a typo'd bankName, the directory's canonical value wins."""
    payment = {"rail": "WIRE", "creditor": {"bic": "BARCGB22"}, "remittance": {}}
    plan = enrichment_plan.EnrichmentPlan()
    enrichment._apply_agent_proposals(
        plan, payment, _ref_with_banks(),
        [{"field": "creditor.bankName", "to": "Barcleys Bank (typo)", "reason": "r",
          "confidence": "HIGH", "source": "agent"}],
        _now(), external_creditor=True,
    )
    assert plan.updates["creditor.bankName"] == "Barclays Bank PLC"  # canonical, not restated


def test_customer_supplied_bank_field_wins_over_agent():
    payment = {"rail": "WIRE",
               "creditor": {"bic": "BARCGB22", "bankName": "Customer Supplied Bank"},
               "remittance": {}}
    plan = enrichment_plan.EnrichmentPlan()
    enrichment._apply_agent_proposals(
        plan, payment, _ref_with_banks(),
        [{"field": "creditor.bankName", "to": "Barclays Bank PLC", "reason": "r",
          "confidence": "HIGH", "source": "agent"},
         {"field": "creditor.bankCountry", "to": "GB", "reason": "r",
          "confidence": "HIGH", "source": "agent"}],
        _now(), external_creditor=True,
    )
    # The customer's bankName is untouched; the agent filled only the gap (bankCountry).
    assert "creditor.bankName" not in plan.updates
    assert plan.updates["creditor.bankCountry"] == "GB"


def test_agent_bic_not_in_directory_warns_and_applies_nothing():
    payment = {"rail": "WIRE", "creditor": {"bic": "NOPEGB00"}, "remittance": {}}
    plan = enrichment_plan.EnrichmentPlan()
    block = enrichment._apply_agent_proposals(
        plan, payment, _ref_with_banks(),
        [{"field": "creditor.bankName", "to": "Nope Bank", "reason": "r",
          "confidence": "HIGH", "source": "agent"}],
        _now(), external_creditor=True,
    )
    assert "creditor.bankName" not in plan.updates
    assert any(o[0] == "beneficiary_bank_resolved" and o[1] == "WARN" for o in plan.outcomes)
    # No bank proposal in the agent block (nothing applied). block may be None or empty-list.
    assert block is None or not [p for p in block["proposals"]
                                 if p["field"].startswith("creditor.")]


def test_internal_creditor_bank_proposals_are_not_applied():
    """Regression: an internal creditor's bank identity is the deterministic planner's job
    (`_plan_internal_creditor_bank`, source=bank_identity). The agent's bank section must be
    skipped for internal creditors, or it would double-write `creditor.clearingSystem*`
    (bank_identity row + agent row) — the customer-supplied-wins gate reads the original doc,
    not the planner's pending updates, so it would not catch the collision."""
    payment = {"rail": "WIRE", "creditor": {"bic": "BARCGB22"}, "remittance": {}}
    plan = enrichment_plan.EnrichmentPlan()
    # Simulate the planner's internal stamping having run (it always does).
    plan._set("creditor.clearingSystemMemberId", "OUR-ABA",
              before=None, source="bank_identity")
    enrichment._apply_agent_proposals(
        plan, payment, _ref_with_banks(),
        [{"field": "creditor.clearingSystemMemberId", "to": "ABC123", "reason": "from BIC",
          "confidence": "HIGH", "source": "agent"}],
        _now(), external_creditor=False,  # internal creditor
    )
    # No agent row added; the planner's bank_identity row is the only one.
    agent_rows = [r for r in plan.resolved if r["source"] == "agent"
                  and r["field"].startswith("creditor.")]
    assert not agent_rows
    assert plan.updates["creditor.clearingSystemMemberId"] == "OUR-ABA"


# --- the agent-first orchestration: planner split + fallback -----------------
# The decision lives in `enrichment_plan.plan(skip_external_creditor_bank=...)` and the
# public `plan_external_creditor_bank` fallback. Pinning them at the planner layer (pure,
# no collection) matches this repo's discipline — `enrichment.run`'s 3-line orchestration
# wires them together and is covered by the conformance suite.

def test_plan_skip_flag_omits_external_bank_resolution():
    """When the agent is configured, plan() skips the external directory lookup so the agent
    is the genuine owner. The internal bank_identity stamping still runs."""
    payment = {"rail": "WIRE", "creditor": {"bic": "BARCGB22"}, "remittance": {}}
    plan = enrichment_plan.plan(
        payment, _ref_with_banks(), external_creditor=True,
        skip_external_creditor_bank=True,
    )
    bank_rows = [r for r in plan.resolved if r["field"].startswith("creditor.")]
    assert not bank_rows, "external bank resolution must be skipped when the agent owns it"
    assert "creditor.bankName" not in plan.updates


def test_plan_without_skip_resolves_external_bank_deterministically():
    """Default (agent down): plan() resolves the external bank — today's behavior."""
    payment = {"rail": "WIRE", "creditor": {"bic": "BARCGB22"}, "remittance": {}}
    plan = enrichment_plan.plan(
        payment, _ref_with_banks(), external_creditor=True,
        skip_external_creditor_bank=False,
    )
    bank_rows = [r for r in plan.resolved if r["field"].startswith("creditor.")]
    assert bank_rows
    assert all(r["source"] == "correspondentBanks" for r in bank_rows)


def test_plan_external_creditor_bank_is_the_fallback():
    """The public fallback an agent-configured run calls when the agent proposed no usable
    bank. It resolves the external bank with source=correspondentBanks."""
    payment = {"rail": "WIRE", "creditor": {"bic": "BARCGB22"}, "remittance": {}}
    plan = enrichment_plan.EnrichmentPlan()
    enrichment_plan.plan_external_creditor_bank(
        plan, payment, _ref_with_banks(), external_creditor=True, rail="WIRE",
    )
    bank_rows = [r for r in plan.resolved if r["field"].startswith("creditor.")]
    assert bank_rows
    assert all(r["source"] == "correspondentBanks" for r in bank_rows)
    assert plan.updates["creditor.bankName"] == "Barclays Bank PLC"


def test_internal_creditor_bank_identity_always_runs():
    """The internal bank_identity stamping is NOT agent territory — it runs regardless of the
    skip flag (our own identity, deterministic)."""
    payment = {"rail": "WIRE", "creditor": {"bic": "OURBIC"}, "remittance": {}}
    plan = enrichment_plan.plan(
        payment, _ref_with_banks(), external_creditor=False,
        skip_external_creditor_bank=True,
    )
    # Internal creditor on an interbank rail → our clearing member id is stamped.
    assert plan.updates.get("creditor.clearingSystemMemberId") is not None
    assert all(r["source"] == "bank_identity" for r in plan.resolved
               if r["field"].startswith("creditor."))
