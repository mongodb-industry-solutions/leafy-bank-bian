"""Stage 3 steps 5–6 — enrichment, the as-captured diff, and post-enrichment revalidation.

Doc 17 §3 steps 5 and 6. Four groups:

1. **`enrichment_plan`** — pure planning against `InMemoryReferenceData`, the port's own
   reference implementation.
2. **The `enrichment{}` record** (B1) — `original{}` is genuinely pre-enrichment, `resolved[]`
   carries `{field, from, to, source}`, and a no-op resolution never appears in the diff.
3. **`_final_validate`** (step 6) — what refuses and what warns, with the refusal rule
   asserted against the spec file rather than transcribed.
4. **End to end through the saga** — the lifecycle events, and the internal-transfer
   regression.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contexts.payment_order_initiation.domain import (
    bank_identity,
    enrichment_plan,
)
from contexts.payment_order_initiation.ports.reference_data import (
    BankRecord,
    InMemoryReferenceData,
    NullReferenceData,
    PurposeCodeRecord,
)

SPEC = Path(__file__).resolve().parents[4] / "doinas-research" / "propose_payments.json"

CHASE = BankRecord(
    bic="CHASUS33",
    bank_name="JPMorgan Chase Bank, N.A.",
    bank_country="US",
    clearing_system_code="USABA",
    clearing_system_member_id="121000248",
    city="New York",
)
BARCLAYS = BankRecord(
    bic="BARCGB22", bank_name="Barclays Bank PLC", bank_country="GB",
    clearing_system_code="GBDSC", clearing_system_member_id="202053",
)
SUPP = PurposeCodeRecord(
    code="SUPP", name="Supplier Payment",
    description="Payment to a supplier against an invoice.", category="Trade",
)


def _store(banks=(CHASE, BARCLAYS), codes=(SUPP,)):
    return InMemoryReferenceData(banks=list(banks), purpose_codes=list(codes))


def _wire(**over) -> dict:
    """A WIRE payment as stage 1 persists it: thin creditor, nothing resolved.

    This is deliberately the "before" half of Doina's L459-460 screen — a name and an
    account number, and little else.
    """
    doc = {
        "paymentId": "PAY-0001",
        "rail": "WIRE",
        "type": "CREDIT_TRANSFER",
        "currency": "USD",
        "chargeBearer": "SHAR",
        "categoryPurpose": "SUPP",
        "fees": [],
        "debtor": {
            "accountId": "ACC-debtor01",
            "bic": bank_identity.OUR_BIC,
            "bankName": bank_identity.OUR_BANK_NAME,
            "bankCountry": bank_identity.OUR_BANK_COUNTRY,
            "clearingSystemMemberId": None,
            "clearingSystemCode": None,
        },
        "creditor": {
            "accountId": None,
            "accountNo": "123456",
            "name": "Supplier XYZ",
            "bic": "CHASUS33",
            "bankName": None,
            "bankCountry": None,
            "clearingSystemMemberId": None,
            "clearingSystemCode": None,
        },
        "remittance": {"reference": "INV-48392", "purposeCode": None},
        "wireDetails": {"wireType": None, "initiatingParty": None},
        "authentication": {"callerType": "CUSTOMER"},
        "initiation": {"initiatedBy": "CUST-abc10001"},
    }
    doc.update(over)
    return doc


# --------------------------------------------------------------------------- #
# 1. enrichment_plan
# --------------------------------------------------------------------------- #

def test_the_beneficiary_bank_is_resolved_from_the_directory():
    plan = enrichment_plan.plan(_wire(), _store(), external_creditor=True)
    assert plan.updates["creditor.bankName"] == "JPMorgan Chase Bank, N.A."
    assert plan.updates["creditor.bankCountry"] == "US"
    assert plan.updates["creditor.clearingSystemMemberId"] == "121000248"
    assert plan.updates["creditor.clearingSystemCode"] == "USABA"


def test_our_own_routing_number_is_stamped_on_the_debtor():
    """D10: the ORIGINATING bank had no routing number, so a domestic wire was unsendable."""
    plan = enrichment_plan.plan(_wire(), _store(), external_creditor=True)
    assert plan.updates["debtor.clearingSystemMemberId"] == bank_identity.OUR_ABA
    assert plan.updates["debtor.clearingSystemCode"] == "USABA"


def test_an_unresolvable_bank_warns_and_resolves_nothing():
    """B7: one absent directory row must not fail a wire."""
    plan = enrichment_plan.plan(_wire(), _store(banks=()), external_creditor=True)
    assert "creditor.bankName" not in plan.updates
    name, result, detail = next(
        o for o in plan.outcomes if o[0] == "beneficiary_bank_resolved"
    )
    assert result == "WARN" and "not in the institution directory" in detail


def test_a_bank_can_be_resolved_by_routing_number_when_no_bic_was_given():
    payment = _wire()
    payment["creditor"]["bic"] = None
    payment["creditor"]["clearingSystemCode"] = "USABA"
    payment["creditor"]["clearingSystemMemberId"] = "121000248"

    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)
    assert plan.updates["creditor.bic"] == "CHASUS33"


def test_the_purpose_code_is_validated_against_the_table():
    """R16 — her 'formal reference table … not just a free-text field'."""
    plan = enrichment_plan.plan(_wire(), _store(), external_creditor=True)
    assert plan.updates["remittance.purposeCode"] == "SUPP"
    name, result, detail = next(o for o in plan.outcomes if o[0] == "purpose_code_resolved")
    assert result == "PASS" and "Supplier Payment" in detail


def test_an_unknown_purpose_code_warns_and_is_carried_as_supplied():
    plan = enrichment_plan.plan(
        _wire(categoryPurpose="ZZZZ"), _store(), external_creditor=True
    )
    _, result, detail = next(o for o in plan.outcomes if o[0] == "purpose_code_resolved")
    assert result == "WARN" and "not in the reference table" in detail
    assert "categoryPurpose" not in plan.updates


def test_no_purpose_code_is_a_skip_not_an_inference():
    payment = _wire(categoryPurpose=None)
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)
    _, result, _ = next(o for o in plan.outcomes if o[0] == "purpose_code_resolved")
    assert result == "SKIP"


def test_a_wire_fee_is_recorded_and_the_amount_is_untouched():
    """Doc 17 §7 watch item 1: `amount` is the ledger's input. A fee is recorded, not deducted."""
    payment = _wire(amount=25_000.0, instructedAmount=25_000.0)
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)

    fee = plan.updates["fees"][0]
    assert fee["amount"] == enrichment_plan.WIRE_FEE
    assert fee["currency"] == "USD"
    assert fee["chargedTo"] == "SHARED"        # chargeBearer SHAR
    assert "amount" not in plan.updates
    assert "instructedAmount" not in plan.updates
    assert "currency" not in plan.updates


def test_an_internal_transfer_attracts_no_fee():
    plan = enrichment_plan.plan(
        _wire(rail="INTERNAL"), _store(), external_creditor=False
    )
    assert "fees" not in plan.updates
    _, result, _ = next(o for o in plan.outcomes if o[0] == "charges_calculated")
    assert result == "SKIP"


def test_the_initiating_party_comes_from_the_authenticated_caller():
    """R20 / FATF R.16 — answerable only since Level-1 auth mints a real `callerType`."""
    plan = enrichment_plan.plan(_wire(), _store(), external_creditor=True)
    party = plan.updates["wireDetails.initiatingParty"]
    assert party["name"] == "CUST-abc10001"
    assert "CUSTOMER" in party["identification"]


def test_an_operator_initiated_payment_is_distinguishable():
    """The whole point of R20: the submitter is not automatically the account holder."""
    payment = _wire()
    payment["authentication"]["callerType"] = "OPERATOR"
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)
    assert "OPERATOR" in plan.updates["wireDetails.initiatingParty"]["identification"]


def test_the_priority_projects_to_the_wire_service_level():
    """FR-1.6 detail — the envelope's pain.001 alignment: priority → PmtTpInf/SvcLvl."""
    plan = enrichment_plan.plan(_wire(), _store(), external_creditor=True)
    assert plan.updates["wireDetails.paymentTypeInformation.serviceLevel.code"] == "NORM"


def test_an_urgent_wire_is_an_urgent_payment():
    """URGENT maps to URGP (ISO ExternalServiceLevel1Code 'urgent payment')."""
    plan = enrichment_plan.plan(_wire(priority="URGENT"), _store(), external_creditor=True)
    assert plan.updates["wireDetails.paymentTypeInformation.serviceLevel.code"] == "URGP"


def test_a_high_priority_wire_is_same_day_value():
    """HIGH maps to SDVA — a high-priority wire is same-day value."""
    plan = enrichment_plan.plan(_wire(priority="HIGH"), _store(), external_creditor=True)
    assert plan.updates["wireDetails.paymentTypeInformation.serviceLevel.code"] == "SDVA"


def test_a_caller_supplied_service_level_is_never_overwritten():
    """The enrichment must not clobber a stage-1 caller value (or a stage-4 one)."""
    payment = _wire(priority="URGENT")
    payment["wireDetails"] = {
        "wireType": None,
        "initiatingParty": None,
        "paymentTypeInformation": {"serviceLevel": {"code": "SDVA"}},
    }
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)
    assert "wireDetails.paymentTypeInformation.serviceLevel.code" not in plan.updates


def test_a_null_reference_store_resolves_nothing_and_raises_nothing():
    plan = enrichment_plan.plan(_wire(), NullReferenceData(), external_creditor=True)
    # Our own side still resolves — it comes from a constant, not a lookup.
    assert plan.updates["debtor.clearingSystemMemberId"] == bank_identity.OUR_ABA
    assert "creditor.bankName" not in plan.updates


# --------------------------------------------------------------------------- #
# 1b. FR-3.12 — regulatory reports
# --------------------------------------------------------------------------- #

def _reg_outcome(plan):
    return next(o for o in plan.outcomes if o[0] == "regulatory_reports_assessed")


def test_a_cross_border_wire_attracts_a_regulatory_declaration():
    """FR-3.12 — a WIRE with wireType INTERNATIONAL gets a CROSS_BORDER_DECLARATION."""
    payment = _wire()
    payment["wireDetails"]["wireType"] = "INTERNATIONAL"
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)

    reports = plan.updates["correspondent.regulatoryReports"]
    assert [r["reportType"] for r in reports] == ["CROSS_BORDER_DECLARATION"]
    r = reports[0]
    assert r["authority"] == enrichment_plan.REGULATORY_AUTHORITY
    assert r["status"] == "REQUIRED"
    assert r["simulated"] is True
    assert r["thresholdAmount"] is None
    # The plan is pure — `assessedAt` is null until enrichment.run stamps it.
    assert r["assessedAt"] is None
    _, result, _ = _reg_outcome(plan)
    assert result == "PASS"


def test_a_high_value_wire_attracts_a_threshold_report():
    """FR-3.12 — a WIRE at or above the threshold gets a THRESHOLD_REPORT carrying it."""
    payment = _wire(amount=25_000.0, instructedAmount=25_000.0)
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)

    reports = plan.updates["correspondent.regulatoryReports"]
    assert [r["reportType"] for r in reports] == ["THRESHOLD_REPORT"]
    assert reports[0]["thresholdAmount"] == enrichment_plan.regulatory_report_threshold()


def test_a_cross_border_high_value_wire_gets_both_reports():
    """Both triggers can fire on one payment — two entries, order is stable."""
    payment = _wire(amount=25_000.0, instructedAmount=25_000.0)
    payment["wireDetails"]["wireType"] = "INTERNATIONAL"
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)

    reports = plan.updates["correspondent.regulatoryReports"]
    assert [r["reportType"] for r in reports] == [
        "CROSS_BORDER_DECLARATION", "THRESHOLD_REPORT",
    ]
    _, result, detail = _reg_outcome(plan)
    assert result == "PASS" and "2 regulatory report(s)" in detail


def test_a_domestic_wire_under_threshold_gets_no_report():
    """The spec's "Empty array if none" — no $set, so the diff stays clean."""
    payment = _wire(amount=5_000.0, instructedAmount=5_000.0)
    payment["wireDetails"]["wireType"] = "DOMESTIC"
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)

    assert "correspondent.regulatoryReports" not in plan.updates
    _, result, _ = _reg_outcome(plan)
    assert result == "SKIP"


def test_an_internal_transfer_gets_no_regulatory_report():
    """A book transfer reaches no clearing system — SKIP, not a refusal."""
    plan = enrichment_plan.plan(
        _wire(rail="INTERNAL", amount=25_000.0), _store(), external_creditor=False
    )
    assert "correspondent.regulatoryReports" not in plan.updates
    _, result, detail = _reg_outcome(plan)
    assert result == "SKIP" and "INTERNAL" in detail


def test_the_threshold_is_env_configurable(monkeypatch):
    """Lazy env read — raising the threshold lifts the threshold report off a $25k wire."""
    monkeypatch.setenv("REGULATORY_REPORT_THRESHOLD_USD", "50000")
    payment = _wire(amount=25_000.0, instructedAmount=25_000.0)
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)

    assert "correspondent.regulatoryReports" not in plan.updates
    _, result, _ = _reg_outcome(plan)
    assert result == "SKIP"


def test_the_plan_does_not_stamp_the_assessment_time():
    """The plan is pure (no clock) — `assessedAt` is null until enrichment.run owns `now`.

    Same class as the mapper purity test: enrichment must be deterministic given its inputs,
    so a timestamp can only be stamped by the caller that owns the clock."""
    payment = _wire()
    payment["wireDetails"]["wireType"] = "INTERNATIONAL"
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)

    for report in plan.updates["correspondent.regulatoryReports"]:
        assert report["assessedAt"] is None


# --------------------------------------------------------------------------- #
# 2. The enrichment{} record (B1)
# --------------------------------------------------------------------------- #

def test_a_no_op_resolution_never_appears_in_the_diff():
    """A before/after panel showing rows where nothing changed is unreadable."""
    payment = _wire()
    payment["creditor"].update(
        bankName="JPMorgan Chase Bank, N.A.", bankCountry="US",
        clearingSystemMemberId="121000248", clearingSystemCode="USABA",
    )
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)
    assert not [e for e in plan.resolved if e["field"].startswith("creditor.bank")]


def test_every_resolution_names_where_the_value_came_from():
    plan = enrichment_plan.plan(_wire(), _store(), external_creditor=True)
    assert plan.resolved
    for entry in plan.resolved:
        assert set(entry) == {"field", "from", "to", "source"}
        assert entry["source"]
        assert entry["from"] != entry["to"]


def test_the_original_snapshot_holds_the_pre_enrichment_values():
    """B1 — the 'before' half of her L459-460 screen, and the only place it survives."""
    payment = _wire()
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)
    original = enrichment_plan.snapshot_original(payment, list(plan.updates))

    assert original["creditor"]["bankName"] is None
    assert original["creditor"]["clearingSystemMemberId"] is None
    assert original["debtor"]["clearingSystemMemberId"] is None
    assert original["remittance"]["purposeCode"] is None


def test_the_snapshot_covers_exactly_the_fields_that_changed():
    payment = _wire()
    plan = enrichment_plan.plan(payment, _store(), external_creditor=True)
    original = enrichment_plan.snapshot_original(payment, list(plan.updates))

    flat = set()

    def walk(node, prefix=""):
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict) and value and all(
                isinstance(v, (dict, str, int, float, type(None), list))
                for v in value.values()
            ) and f"{path}." in " ".join(plan.updates):
                walk(value, f"{path}.")
            else:
                flat.add(path)

    walk(original)
    assert flat == set(plan.updates)


# --------------------------------------------------------------------------- #
# 3. Post-enrichment revalidation (step 6)
# --------------------------------------------------------------------------- #

def test_the_wire_bic_rule_matches_the_specs_own_expr():
    """Asserted against the spec file rather than transcribed into a comment.

    Phase D: a rule copied into code drifts from the spec silently. This is the
    `validator.$and[1]` cross-field constraint the collection validator would enforce if it
    were applied (it is not — Doina Q9).
    """
    if not SPEC.exists():
        pytest.skip("propose_payments.json not available in this checkout")
    validator = json.loads(SPEC.read_text())["collections"]["payments"]["validator"]
    expr = validator["$and"][1]["$expr"]["$cond"]

    assert expr[0] == {"$eq": ["$rail", "WIRE"]}
    required_non_null = {
        list(clause["$ne"])[0] for clause in expr[1]["$and"]
    }
    assert required_non_null == {"$debtor.bic", "$creditor.bic"}


# --------------------------------------------------------------------------- #
# 4. End to end through the saga
# --------------------------------------------------------------------------- #

from test_payments_service import (  # noqa: E402
    EXTERNAL_CREDITOR,
    _checks,
    _initiate,
    _initiate_external,
    _one,
    db,          # noqa: F401
    service,     # noqa: F401
)


def _states(db):
    payment = db["payments"].docs[-1]
    return [e["state"] for e in payment["lifecycle"]["events"]]


def test_an_internal_transfer_walks_enriched_and_final_validated(service, db):
    _initiate(service)
    states = _states(db)
    assert "ENRICHED" in states and "FINAL_VALIDATED" in states


def test_an_internal_transfer_reports_the_specs_own_intrabank_wording(service, db):
    """The spec's `internal_transfer` sample uses exactly this reason."""
    _initiate(service)
    payment = db["payments"].docs[-1]
    enriched = next(
        e for e in payment["lifecycle"]["events"] if e["state"] == "ENRICHED"
    )
    assert "intrabank transfer" in enriched["reason"]


def test_an_internal_transfer_records_an_empty_diff(service, db):
    """Nothing to resolve, so `resolved[]` is empty — but `enrichment{}` still exists."""
    _initiate(service)
    payment = db["payments"].docs[-1]
    assert payment["enrichment"] is not None
    assert payment["enrichment"]["resolved"] == []
    assert payment["enrichment"]["actor"] == "transactions-service"


def test_an_internal_transfer_still_settles(service, db):
    """The regression that matters: stage 3 must not break the happy path."""
    result = _initiate(service)
    assert result["status"] == "SETTLED"


def test_the_final_validation_check_is_skipped_off_the_wire_rail(service, db):
    _initiate(service)
    entry = _one(db, "wire_agents_complete")
    assert entry["result"] == "SKIP"
    assert entry["stage"] == "3 final-validate"


def test_an_external_wire_warns_when_the_directory_is_empty(service, db):
    """The fake db has no `correspondentBanks`, so nothing resolves — and nothing refuses.

    This is B7's whole claim, exercised: a thin directory degrades the payment, it does not
    fail it. The wire still halts at stage 5, as it did before stage 3 existed.
    """
    _initiate_external(service)

    bank = _one(db, "beneficiary_bank_resolved")
    assert bank["result"] == "WARN"

    agents = _one(db, "wire_agents_complete")
    assert agents["result"] == "WARN"
    assert "not in the institution directory" in agents["detail"]

    assert "FINAL_VALIDATED" in _states(db)


def test_an_external_wire_still_gets_our_own_routing_number(service, db):
    """Our side comes from a constant, so it resolves even with no directory at all."""
    _initiate_external(service)
    payment = db["payments"].docs[-1]
    assert payment["debtor"]["clearingSystemMemberId"] == bank_identity.OUR_ABA
    assert [
        e for e in payment["enrichment"]["resolved"]
        if e["field"] == "debtor.clearingSystemMemberId"
    ]


def test_the_external_creditor_bic_survives_enrichment(service, db):
    """A resolution must never blank a value the caller supplied."""
    _initiate_external(service)
    payment = db["payments"].docs[-1]
    assert payment["creditor"]["bic"] == EXTERNAL_CREDITOR["bic"]


def test_stage_three_checks_span_both_halves(service, db):
    """Ten from `3 validate`, then the enrichment half, then final validation."""
    _initiate(service)
    stages = {c["stage"] for c in _checks(db) if c["stage"].startswith("3 ")}
    assert stages == {"3 validate", "3 enrich", "3 final-validate"}
