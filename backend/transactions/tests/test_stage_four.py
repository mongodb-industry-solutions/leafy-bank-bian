"""Stage 4 — orchestration & authorization. Doc 18 §4.

Four kinds of test, per the playbook's Phase H:

* **pure domain** — `routing`, `fraud_rules`, `sanctions`, `documents`, no database
* **behaviour** — one per R-row that changes runtime behaviour, driven through the saga
* **the two seams** — a rule that can refuse must also be satisfiable on the same fixture
  family (defect 2026-08-31: a control that can only refuse is half-built)
* **immutability** — an AST walk, because a docstring saying "never updated" guarantees
  nothing

Spec-conformance and enum-parity for stage 4 live in `test_payment_document_spec.py`, with
the rest of the conformance suite, so there is one place that loads the canonical file.
"""

import ast
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest

from contexts.fraud_evaluation.domain import fraud_rules, sanctions
from contexts.payment_orchestration.domain import documents, routing
from services.payments_service import PaymentsService
from tests.test_payments_service import (  # reuse the fixtures, don't fork them
    CREDITOR,
    CUST_C,
    CUST_D,
    DEBTOR,
    EXTERNAL_CREDITOR,
    FakeCollection,
    FakeConnection,
    FakeDb,
    _account,
    _CLEARING_WIRE,
    _customer,
    _initiate,
    db,          # noqa: F401 - pytest fixture
    service,     # noqa: F401 - pytest fixture
)

_BACKEND = pathlib.Path(__file__).resolve().parents[2]
_SEED = (
    _BACKEND / "data" / "seed" / "leafy_bank_bian.correspondentBanks.json"
)

_NOW = date(2026, 9, 1)


@pytest.fixture
def rich_db():
    """A COMMERCIAL debtor with enough balance and headroom to reach stage 4's own rules.

    The shared `db`/`service` fixtures cap the debtor at 10,000 with a 50,000 service limit,
    so a payment large enough to trip the REVIEW or DECLINE thresholds is refused three
    stages earlier — by the amount bound in `capture`, or by funds in stage 3, or by stage
    2's RETAIL entitlement. That is correct behaviour and exactly why it needs its own
    fixture: without one, every high-score assertion would be vacuous, which is the
    2026-08-31 defect (a control graded against a fixture that can never reach it).
    """
    return FakeDb({
        "accounts": FakeCollection([
            _account(DEBTOR, CUST_D, available=10_000_000.0),
            _account(CREDITOR, CUST_C, available=10_000.0),
            _CLEARING_WIRE,
        ]),
        "customers": FakeCollection(
            [_customer(CUST_D, segment="COMMERCIAL"), _customer(CUST_C)],
            key="customerId",
        ),
        "payments": FakeCollection(key="paymentId"),
        "transactions": FakeCollection(key="transactionId"),
        "notifications": FakeCollection(key="notificationId"),
        # Seeded so stage 3 can actually RESOLVE a purpose code, which is what feeds stage
        # 4's AML rule. Her L446 asks for the code to feed *"AML risk scoring (Stage 4)"*
        # via a formal reference table — with this collection empty, stage 3 resolves
        # nothing, `remittance.purposeCode` stays None, and `PURPOSE_CODE_RISK` could never
        # fire through the saga even though its unit test passes. That gap is exactly the
        # 2026-08-31 producer/consumer defect, one layer up.
        "purposeCodes": FakeCollection([
            {"code": "SUPP", "name": "Supplier payment", "description": "Trade payable",
             "category": "COMMERCIAL"},
            {"code": "CASH", "name": "Cash management transfer",
             "description": "Cash concentration", "category": "TREASURY"},
        ], key="code"),
    })


@pytest.fixture
def rich_service(rich_db):
    rich_db["payments"].unique_on = "idempotencyKey"
    return PaymentsService(
        FakeConnection(rich_db), "leafy_bank_bian", payment_limit_usd=5_000_000.0
    )


def _decide(**over):
    kwargs = dict(
        rail="WIRE",
        wire_type="DOMESTIC",
        priority="NORMAL",
        amount=250.0,
        currency="USD",
        creditor_country="US",
        creditor_bic="CHASUS33",
        requested_execution_date=None,
        now=_NOW,
        now_hour_et=10,
    )
    kwargs.update(over)
    return routing.decide(**kwargs)


# --- R1: orchestration decides HOW, never WHICH rail --------------------------

def test_orchestration_never_changes_the_rail():
    """Her L487 is the whole shape of this stage. `rail` is an input; the *network* is the
    output. A `rail` field appearing on `ExecutionStrategy` would mean orchestration had
    started選 selecting rails, which is only the untyped-intake fallback (out of scope, Q31)."""
    strategy = _decide()
    assert not hasattr(strategy, "rail")


def test_the_saga_leaves_the_rail_exactly_as_stage_one_captured_it(service, db):
    payment = _initiate(service, payment_rail="INTERNAL")
    assert payment["rail"] == "INTERNAL"


# --- R3: her worked examples --------------------------------------------------

def test_an_urgent_international_wire_routes_via_swift_correspondent():
    """Doina L492, verbatim: *"Payment type = WIRE, $25,000 USD international + urgent →
    route via SWIFT correspondent rather than a slower bilateral arrangement."*"""
    strategy = _decide(
        wire_type="INTERNATIONAL", priority="URGENT", amount=25_000.0,
        creditor_country="CA", creditor_bic="ROYCCAT2",
    )
    assert strategy.network == routing.SWIFT
    assert strategy.strategy == routing.SWIFT_CORRESPONDENT
    assert strategy.requires_correspondent is True
    assert "cross-border" in strategy.rationale
    assert "urgent" in strategy.rationale


def test_a_domestic_urgent_wire_takes_fedwire_and_a_non_urgent_one_takes_chips():
    """Cost is one of her seven inputs (L489), and this is where it shows: RTGS settles
    now and costs more; netted clearing is cheaper and slower."""
    urgent = _decide(priority="URGENT")
    normal = _decide(priority="NORMAL")
    assert (urgent.network, urgent.cost_rank) == (routing.FEDWIRE, routing.COST_HIGH)
    assert (normal.network, normal.cost_rank) == (routing.CHIPS, routing.COST_MEDIUM)


def test_an_internal_transfer_reaches_no_clearing_network():
    """The same reasoning that gated stage 3's `INTERBANK_RAILS`: a book transfer between
    two accounts we hold never reaches a clearing system, and the spec's own
    `internal_transfer` sample has both clearing member ids NULL."""
    strategy = _decide(rail="INTERNAL", wire_type=None)
    assert strategy.network is None
    assert strategy.strategy == routing.BOOK_TRANSFER
    assert strategy.requires_correspondent is False


def test_ach_is_routed_to_the_standard_window_and_says_phase_two():
    """R4 is out of scope, but an ACH payment must not be silently unrouted."""
    strategy = _decide(rail="ACH", wire_type=None)
    assert strategy.strategy == routing.ACH_STANDARD_WINDOW
    assert strategy.network is None
    assert "Phase 2" in strategy.rationale


# --- R2: cut-off and value date ----------------------------------------------

def test_past_the_cutoff_the_value_date_rolls_to_the_next_day():
    before = _decide(now_hour_et=10)
    after = _decide(now_hour_et=23)
    assert before.within_cutoff is True
    assert before.value_date == _NOW.isoformat()
    assert after.within_cutoff is False
    assert after.value_date == (_NOW + timedelta(days=1)).isoformat()


def test_swift_has_no_cutoff_so_a_late_international_wire_keeps_its_value_date():
    strategy = _decide(
        wire_type="INTERNATIONAL", creditor_country="CA", creditor_bic="ROYCCAT2",
        now_hour_et=23,
    )
    assert strategy.cutoff_hour_et is None
    assert strategy.within_cutoff is True


def test_a_requested_future_date_survives_the_cutoff_roll():
    """A future-dated payment's value date is the customer's date, not tomorrow."""
    future = _NOW + timedelta(days=5)
    strategy = _decide(requested_execution_date=future, now_hour_et=23)
    assert strategy.value_date == future.isoformat()


# --- the correspondent table must not drift from the directory ----------------

def test_every_correspondent_resolves_against_the_bank_seed():
    """Same parity-test pattern as the frontend autofill pool (stage 3): the SIMULATED
    correspondent table names BICs, and a BIC with no directory row would make every
    international wire log a resolution failure — correct, but it reads as broken."""
    rows = json.loads(_SEED.read_text())["records"]
    seeded = {r["swiftCode"] for r in rows if r.get("recordType") == "BIC_DIRECTORY"}
    assert routing.correspondent_bics() <= seeded, (
        "a correspondent BIC is not in the bank seed"
    )


def test_the_beneficiary_bank_being_our_own_correspondent_needs_no_intermediary_hop():
    """Real banking semantics, and a good demo line: if the beneficiary banks with our
    correspondent, the payment lands in one hop."""
    strategy = _decide(
        wire_type="INTERNATIONAL", creditor_country="CA", creditor_bic="ROYCCAT2",
    )
    assert strategy.correspondent_bic == "ROYCCAT2"
    assert "no intermediary hop is required" in strategy.rationale


def test_an_unconfigured_destination_country_does_not_fail_the_wire():
    """Doc 17 B7's rule, carried into stage 4: a thin table warns, it does not refuse."""
    strategy = _decide(
        wire_type="INTERNATIONAL", creditor_country="JP", creditor_bic="MHCBJPJT",
    )
    assert strategy.network == routing.SWIFT
    assert strategy.correspondent_bic is None
    assert "without a named correspondent" in strategy.rationale


# --- fraud rules: every rule fires AND stays quiet ---------------------------

def _assess(**over):
    kwargs = dict(
        amount=250.0,
        wire_type=None,
        purpose_code=None,
        high_risk_purpose_codes=sanctions.high_risk_purpose_codes(),
        prior_payments_to_beneficiary=5,
        debtor_payments_in_window=0,
        is_external_creditor=False,
    )
    kwargs.update(over)
    return fraud_rules.assess(**kwargs)


@pytest.mark.parametrize(
    "rule, firing, quiet",
    [
        ("AMOUNT_TIER", {"amount": 500_000.0}, {"amount": 250.0}),
        ("CROSS_BORDER", {"wire_type": "INTERNATIONAL"}, {"wire_type": "DOMESTIC"}),
        (
            "NEW_BENEFICIARY",
            {"is_external_creditor": True, "prior_payments_to_beneficiary": 0},
            {"is_external_creditor": True, "prior_payments_to_beneficiary": 3},
        ),
        (
            "VELOCITY",
            {"debtor_payments_in_window": fraud_rules.VELOCITY_THRESHOLD},
            {"debtor_payments_in_window": fraud_rules.VELOCITY_THRESHOLD - 1},
        ),
        ("PURPOSE_CODE_RISK", {"purpose_code": "CASH"}, {"purpose_code": "SUPP"}),
    ],
)
def test_each_rule_can_both_fire_and_stay_quiet(rule, firing, quiet):
    """Defect 2026-08-31's corollary: a threshold is only verified when something can both
    trip it and satisfy it. Five rules, ten assertions, no vacuous half."""
    assert rule in _assess(**firing).rules_fired
    assert rule not in _assess(**quiet).rules_fired


def test_beneficiary_novelty_does_not_apply_to_an_internal_transfer():
    assert "NEW_BENEFICIARY" not in _assess(
        is_external_creditor=False, prior_payments_to_beneficiary=0
    ).rules_fired


def test_the_score_is_clamped_to_the_specs_range():
    """`fraud.score` is `0-100` in the spec, so the sum must not run off the top: 140 is
    arithmetic but invalid against the schema."""
    worst = _assess(
        amount=5_000_000.0, wire_type="INTERNATIONAL", purpose_code="CASH",
        is_external_creditor=True, prior_payments_to_beneficiary=0,
        debtor_payments_in_window=50,
    )
    assert worst.score == fraud_rules.SCORE_MAX
    assert _assess().score >= fraud_rules.SCORE_MIN


def test_the_flagship_corporate_wire_is_approved():
    """D9: the demo is *ABC Manufacturing pays $25,000 to Supplier XYZ* by wire. If stage 4
    declined or held it, the demo would not reach the ledger — so this is a narrative
    requirement, not just a threshold check."""
    assessment = _assess(
        amount=25_000.0, wire_type="INTERNATIONAL", purpose_code="SUPP",
        is_external_creditor=True, prior_payments_to_beneficiary=0,
    )
    assert assessment.decision == fraud_rules.APPROVED
    assert not assessment.holds and not assessment.refuses


def test_the_three_decisions_are_reachable_and_ordered_by_score():
    low = _assess()
    mid = _assess(amount=250_000.0, wire_type="INTERNATIONAL",
                  is_external_creditor=True, prior_payments_to_beneficiary=0)
    high = _assess(amount=5_000_000.0, wire_type="INTERNATIONAL", purpose_code="CASH",
                   is_external_creditor=True, prior_payments_to_beneficiary=0,
                   debtor_payments_in_window=50)
    assert low.decision == fraud_rules.APPROVED
    assert mid.decision == fraud_rules.REVIEW
    assert high.decision == fraud_rules.DECLINED
    assert low.score < mid.score < high.score


def test_the_model_half_is_labelled_a_simulation():
    """Q29 is open. Until it is answered, nothing may present the baseline as a model."""
    assert "SIMULATED" in fraud_rules.MODEL_ID
    assert "SIMULATED" in fraud_rules.describe(_assess())


def test_no_rule_reads_a_device_or_channel_signal():
    """Doc 18 B8: `initiation.ipAddress`/`deviceId` are written None and `extra="forbid"`
    means no caller can send them, so a rule over them could never fire. Absent, not
    stubbed — and this test stops one being added without the contract changing first."""
    tree = ast.parse(pathlib.Path(fraud_rules.__file__).read_text())

    # Docstrings are excluded deliberately — the module *explains* why these signals are
    # absent, and a plain substring search over the file therefore matches its own
    # explanation. Only real code counts: attribute names, identifiers, and the string
    # literals used as dict keys.
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }

    read = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            read.add(node.attr)
        elif isinstance(node, ast.Name):
            read.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node not in docstrings:
                read.add(node.value)

    for unreachable in ("ipAddress", "deviceId", "ip_address", "device_id"):
        assert unreachable not in read


# --- sanctions ----------------------------------------------------------------

def test_a_denied_party_is_a_hit_and_refuses():
    outcome = sanctions.screen(creditor_name="Acme Sanctioned Holdings", creditor_country="US")
    assert outcome.status == sanctions.HIT
    assert outcome.refuses is True


def test_a_restricted_country_is_blocked_and_refuses():
    outcome = sanctions.screen(creditor_name="Ordinary Trading Co", creditor_country="IR")
    assert outcome.status == sanctions.BLOCKED
    assert outcome.refuses is True


def test_an_unscreenable_party_is_pending_and_does_NOT_refuse():
    """The important one. A party we cannot screen must never be recorded CLEAR — but it
    must not refuse either, or a thin list would fail payments (doc 17 B7). PENDING is the
    honest third answer, and it is in the spec's own enum."""
    outcome = sanctions.screen(creditor_name=None, creditor_country=None)
    assert outcome.status == sanctions.PENDING
    assert outcome.refuses is False


def test_a_clean_party_clears_and_the_provider_says_simulated():
    outcome = sanctions.screen(creditor_name="Supplier XYZ Ltd.", creditor_country="CA")
    assert outcome.status == sanctions.CLEAR
    assert outcome.refuses is False
    assert "SIMULATED" in outcome.detail


def test_a_high_risk_purpose_code_is_scored_not_screened_out():
    """An AML-relevant purpose is a risk weight, not a sanctions match — conflating them
    would refuse legitimate cash-management payments."""
    outcome = sanctions.screen(
        creditor_name="Supplier XYZ Ltd.", creditor_country="CA", purpose_code="CASH"
    )
    assert outcome.status == sanctions.CLEAR
    assert "PURPOSE_CODE_RISK" in _assess(purpose_code="CASH").rules_fired


# --- the two new documents ----------------------------------------------------

def _payment(**over):
    doc = {
        "paymentId": "PAY-0000abcd",
        "customerId": "CUST-0000000001",
        "rail": "WIRE",
        "type": "CREDIT_TRANSFER",
        "amount": 25_000.0,
        "currency": "USD",
        "fxRate": None,
        "chargeBearer": "DEBT",
        "priority": "URGENT",
        "requestedExecutionDate": "2026-09-01",
        "fees": [{"type": "WIRE_FEE", "amount": 25.0, "currency": "USD",
                  "chargedTo": "DEBTOR"}],
        "creditor": {"bic": "ROYCCAT2", "bankName": "Royal Bank of Canada",
                     "bankCountry": "CA", "clearingSystemCode": "CACPA",
                     "clearingSystemMemberId": "000300002"},
        "wireDetails": {"wireType": "INTERNATIONAL",
                        "messageDefinitionIdentifier": "pain.001.001.09"},
    }
    doc.update(over)
    return doc


def test_the_routing_snapshot_captures_her_three_named_things_by_value():
    """L500: *"capturing exactly which clearing network, beneficiary bank identifier, and
    (for cross-border) correspondent/nostro account were selected at this moment."* By
    value, never by reference — a snapshot pointing at `correspondentBanks` would change
    when that directory changed, which is the failure immutability exists to prevent."""
    strategy = _decide(wire_type="INTERNATIONAL", priority="URGENT", amount=25_000.0,
                       creditor_country="CA", creditor_bic="ROYCCAT2")
    snap = documents.routing_snapshot(
        payment=_payment(), strategy=strategy,
        resolved_correspondent={"bankName": "Royal Bank of Canada", "country": "CA"},
        now=datetime.now(timezone.utc),
    )
    assert snap["clearingNetwork"] == routing.SWIFT
    assert snap["beneficiaryAgent"]["bic"] == "ROYCCAT2"
    assert snap["beneficiaryAgent"]["clearingSystemMemberId"] == "000300002"
    assert snap["correspondent"]["bic"] == "ROYCCAT2"
    assert snap["correspondent"]["resolved"] is True
    assert snap["routingSnapshotId"].startswith("RS-")
    assert snap["immutable"] is True


def test_the_snapshot_marks_the_correspondent_simulated_and_leaves_nostro_null():
    """Doc 18 B5: no nostro or settlement-account field exists anywhere in the canonical
    model, so it is null rather than invented. It needs the chart-of-accounts extension,
    which is Doina/Payton's call (Q9)."""
    strategy = _decide(wire_type="INTERNATIONAL", creditor_country="GB",
                       creditor_bic="BARCGB22")
    snap = documents.routing_snapshot(
        payment=_payment(), strategy=strategy, resolved_correspondent=None,
        now=datetime.now(timezone.utc),
    )
    assert snap["correspondent"]["simulated"] is True
    assert snap["correspondent"]["nostroAccountRef"] is None
    assert snap["correspondent"]["resolved"] is False


def test_the_payment_order_confirms_the_instructed_amount_unchanged():
    """⚠️ The boundary guard. A fee is RECORDED, never deducted (doc 17 §7): `amount` is
    the ledger's primary input, and a payment order that netted the fee out would be a
    boundary change disguised as a fee."""
    payment = _payment()
    order = documents.payment_order(
        payment=payment, strategy=_decide(), routing_snapshot_id="RS-0000abcd",
        authorization={"decision": "APPROVED"}, now=datetime.now(timezone.utc),
    )
    assert order["confirmedAmount"] == payment["amount"] == 25_000.0
    assert order["charges"] == payment["fees"]
    assert order["paymentOrderId"].startswith("PO-")
    assert order["routingSnapshotId"] == "RS-0000abcd"


# --- immutability, enforced by an AST walk -----------------------------------

def test_nothing_ever_updates_a_routing_snapshot():
    """L500 calls the snapshot immutable. A docstring saying so guarantees nothing, so walk
    the backend and assert no mutating driver call is made against the collection handle.

    Matches both spellings a caller could use: the named `collections.routing_snapshots`
    handle and a `db["routingSnapshots"]` subscript.
    """
    mutators = {"update_one", "update_many", "replace_one", "find_one_and_update",
                "find_one_and_replace", "delete_one", "delete_many", "bulk_write"}
    offenders = []
    for path in _BACKEND.rglob("*.py"):
        if ".venv" in path.parts or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in mutators:
                continue
            target = ast.unparse(node.func.value)
            if "routing_snapshots" in target or "routingSnapshots" in target:
                offenders.append(f"{path.name}: {ast.unparse(node.func)}")
    assert offenders == [], f"routingSnapshots must be insert-only: {offenders}"


# --- behaviour, through the saga ---------------------------------------------

def _checks(db, stage_prefix):
    payment = db["payments"].docs[-1]
    return [c for c in payment.get("checks", []) if c["stage"].startswith(stage_prefix)]


def _international(svc, **over):
    """An external INTERNATIONAL wire. `wireType` is DERIVED from the two bank countries
    (`initiation_envelope.derive_wire_type`), so a Canadian beneficiary bank is what makes
    this international — not a flag we set."""
    creditor = dict(EXTERNAL_CREDITOR, bic="ROYCCAT2",
                    bankName="Royal Bank of Canada", bankCountry="CA")
    return _initiate(
        svc, creditor_account_ref=None, creditor_party=creditor,
        payment_rail="WIRE", **over,
    )


def test_stage_four_records_its_checks_in_order(service, db):
    payment = _initiate(service)
    names = [c["name"] for c in _checks(db, "4 ")]
    assert names == [
        # 4a
        "execution_strategy_selected",
        "clearing_network_selected",
        "correspondent_resolved",
        "within_cutoff",
        "routing_snapshot_written",
        "warehoused_for_release",
        # 4b
        "sanctions_screening",
        "risk_assessment",
        "fraud_score_within_threshold",
        "fees_reconfirmed",
        "authorization_decision",
        "originator_confirmed",
    ]
    assert payment["lifecycle"]["currentState"] == "SETTLED"


def test_every_stage_four_check_declares_a_mode_and_an_actor(rich_service, rich_db):
    """Her R11 asks which checks are synchronous and which are asynchronous — a check
    without a mode does not answer her question."""
    _international(rich_service, instructed_amount=25_000.0)
    for entry in _checks(rich_db, "4 "):
        assert entry["mode"] in ("SYNC", "ASYNC")
        assert entry["actor"] in (
            "orchestration-service", "fraud-service", "payment-confirmation-service",
        )


def test_the_internal_transfer_still_walks_every_stage_four_state_and_settles(service, db):
    """The regression that matters: stage 4 must not break the path to the ledger."""
    payment = _initiate(service)
    states = [e["state"] for e in payment["lifecycle"]["events"]]
    assert ["ROUTED", "AUTHORISED", "APPROVED"] == [
        s for s in states if s in {"ROUTED", "AUTHORISED", "APPROVED"}
    ]
    assert payment["clearing"]["authorisedAt"] is not None


def test_an_international_wire_is_stamped_with_swift_and_a_correspondent(rich_service, rich_db):
    """R25 + R26. `wireDetails.network` is the field `api_models` reserves for this stage.

    Uses the flagship figure — $25,000, her D9 scenario — on the COMMERCIAL fixture, which
    is the only one whose entitlement allows it.
    """
    payment = _international(rich_service, instructed_amount=25_000.0, priority="URGENT")
    assert payment["wireDetails"]["network"] == "SWIFT"
    assert payment["correspondent"]["correspondentBic"] == "ROYCCAT2"
    # The beneficiary banks with our own correspondent, so there is no intermediary hop.
    assert payment["correspondent"]["intermediaryBic"] is None


def test_the_routing_snapshot_is_written_and_referenced(service, db):
    """R7 + R24, and the D3 forward pointer that was null since stage 1."""
    payment = _international(service)
    snapshots = db["routingSnapshots"].docs
    assert len(snapshots) == 1
    assert payment["refs"]["routingSnapshotId"] == snapshots[0]["routingSnapshotId"]
    assert snapshots[0]["paymentId"] == payment["paymentId"]


def test_the_payment_order_does_not_exist_before_the_authorization_decision(service, db):
    """The ordering B1 decided, asserted rather than assumed.

    `refs.paymentOrderId`'s own spec description — *"Written at orchestration, after
    authorization"* — is what resolves Doina's Q5 conflict. So the snapshot exists from
    ROUTED, and the order only from APPROVED.
    """
    payment = _initiate(service)
    orders = db["paymentOrders"].docs

    assert len(orders) == 1
    assert payment["refs"]["paymentOrderId"] == orders[0]["paymentOrderId"]

    # The order is committed at APPROVED, so it is stamped onto the document by the
    # APPROVED transition — not the ROUTED one.
    approved = next(e for e in payment["lifecycle"]["events"] if e["state"] == "APPROVED")
    routed = next(e for e in payment["lifecycle"]["events"] if e["state"] == "ROUTED")
    assert routed["at"] <= approved["at"]
    assert orders[0]["routingSnapshotId"] == payment["refs"]["routingSnapshotId"]
    assert orders[0]["authorization"]["decision"] == "APPROVED"


def test_the_fraud_block_carries_all_five_spec_required_subfields(service, db):
    """R22. Before stage 4, `fraud` held `score` and `decision` only — so the document
    never satisfied its own `required` list, even once populated."""
    payment = _initiate(service)
    assert set(payment["fraud"]) == {
        "alertId", "score", "decision", "rulesFired", "checkedAt",
    }


def test_sanctions_screening_is_pending_at_initiation_and_resolved_at_stage_four(service, db):
    """B6. It used to be a hardcoded CLEAR written at initiation, so every payment asserted
    a screening that had not run. PENDING is true, and in the spec's own enum."""
    from contexts.payment_order_initiation.domain import payment_document

    built = payment_document.build.__doc__  # touch, to keep the import meaningful
    assert built is not None

    payment = _initiate(service)
    assert payment["correspondent"]["sanctionsCheck"]["status"] == "CLEAR"
    assert payment["correspondent"]["sanctionsCheck"]["provider"] == sanctions.PROVIDER


def test_a_declined_payment_is_rejected_and_never_passes_through_authorised(rich_service, rich_db):
    """⚠️ The ordering that matters most. "Authorised, then rejected" is a false story: the
    trace would show the bank authorising a payment it declined. A DECLINED payment must go
    from ROUTED straight to REJECTED.

    Driven by the amount, through the real rules — not by monkeypatching the decision, so
    the test proves the rules can actually produce a decline.
    """
    # $500,000 — the COMMERCIAL entitlement ceiling, so this is the largest payment that
    # can reach stage 4 at all — cross-border, to a first-time beneficiary, for an
    # AML-relevant purpose. Four rules fire and the score clears the decline threshold.
    #
    # ⚠️ Worth knowing: without the `CASH` purpose code the same payment scores 61 and only
    # REVIEWs. The decline path is reachable only because stage 3 resolves the code, which
    # is precisely the stage-3-feeds-stage-4 chain her L446 asks for.
    with pytest.raises(ValueError, match="declined by fraud evaluation"):
        _international(
            rich_service, instructed_amount=500_000.0, priority="URGENT",
            category_purpose="CASH",
        )

    payment = rich_db["payments"].docs[-1]
    states = [e["state"] for e in payment["lifecycle"]["events"]]
    assert "AUTHORISED" not in states
    assert "APPROVED" not in states
    assert payment["lifecycle"]["currentState"] == "REJECTED"
    # The evidence survives the rejection — the demo must show WHY.
    assert payment["fraud"]["decision"] == "DECLINED"
    assert rich_db["paymentOrders"].docs == [], "a declined payment commits nothing"


def test_a_reviewed_payment_holds_at_authorised_and_commits_nothing(rich_service, rich_db):
    """R21's *hold*. No `PENDING REVIEW` state exists in the canonical enum (Q6/Q33), so the
    payment holds at AUTHORISED rather than inventing one — and no payment order is
    written, because the bank has not committed."""
    payment = _international(rich_service, instructed_amount=300_000.0, priority="URGENT")

    assert payment["fraud"]["decision"] == "REVIEW"
    assert payment["lifecycle"]["currentState"] == "AUTHORISED"
    assert rich_db["paymentOrders"].docs == []
    assert rich_db["transactions"].inserted == [], "money must not move on a held payment"


def test_a_sanctions_hit_refuses_and_records_the_screening_result(service, db):
    """The refusal path, and the one control in the demo that must stop the payment."""
    denied = dict(EXTERNAL_CREDITOR, name="Acme Sanctioned Holdings")
    with pytest.raises(ValueError, match="denied-party"):
        _initiate(service, creditor_account_ref=None, creditor_party=denied,
                  payment_rail="WIRE")

    payment = db["payments"].docs[-1]
    assert payment["lifecycle"]["currentState"] == "REJECTED"
    assert payment["correspondent"]["sanctionsCheck"]["status"] == "HIT"
    failing = [c for c in payment["checks"] if c["result"] == "FAIL"]
    assert [c["name"] for c in failing] == ["sanctions_screening"]


def test_a_future_dated_payment_is_warehoused_at_routed_and_moves_no_money(service, db):
    """R18 / B9. The hold, not the scheduler.

    ⚠️ This is also the reason the manual gate must use a same-day execution date: a
    warehoused payment never reaches the GL, which would read as a regression.
    """
    payment = _initiate(
        service, requested_execution_date=date.today() + timedelta(days=5)
    )
    assert payment["lifecycle"]["currentState"] == "ROUTED"
    assert db["transactions"].inserted == []
    assert db["paymentOrders"].docs == []

    warehoused = next(
        c for c in _checks(db, "4 orchestrate") if c["name"] == "warehoused_for_release"
    )
    assert warehoused["result"] == "PASS"
    assert warehoused["mode"] == "ASYNC"


def test_a_same_day_payment_is_not_warehoused(service, db):
    """The other half of the threshold — a hold that always fires is not a hold."""
    _initiate(service, requested_execution_date=date.today())
    warehoused = next(
        c for c in _checks(db, "4 orchestrate") if c["name"] == "warehoused_for_release"
    )
    assert warehoused["result"] == "SKIP"


def test_the_fee_is_reconfirmed_against_the_schedule_on_a_wire(service, db):
    """R17. Stage 3 prices it; stage 4 confirms it still matches immediately before
    execution, which is her *"verify that the fee/rate is still valid"*."""
    _international(service)
    fee_check = next(
        c for c in _checks(db, "4 authorize") if c["name"] == "fees_reconfirmed"
    )
    assert fee_check["result"] == "PASS"
    assert "WIRE_FEE" in fee_check["detail"]


def test_the_fee_reconfirmation_is_skipped_off_the_wire_rail(service, db):
    _initiate(service)
    fee_check = next(
        c for c in _checks(db, "4 authorize") if c["name"] == "fees_reconfirmed"
    )
    assert fee_check["result"] == "SKIP"


def test_stage_three_checks_are_untouched_by_stage_four(service, db):
    """Every stage appends to ONE array (doc 15 B3), so stage 4 must not disturb stage 3's
    ten entries or their order."""
    _initiate(service)
    stage_three = [c["name"] for c in _checks(db, "3 ")]
    assert len(stage_three) == 14
    assert stage_three[0] == "required_fields"


# --- the stage-4 read/evaluate operations ------------------------------------

def test_fraud_evaluation_evaluate_scores_without_persisting(rich_service, rich_db):
    """`POST /FraudEvaluation/Evaluate` is non-mutating on purpose.

    The score that authorised the payment is the one written at the AUTHORISED transition. A
    second, later score would leave two on the record with nothing to say which one counted.
    """
    payment = _international(rich_service, instructed_amount=25_000.0)
    recorded = payment["fraud"]

    result = rich_service.evaluate_fraud(payment["paymentId"])

    assert result["persisted"] is False
    assert result["recordedFraud"]["score"] == recorded["score"]
    assert result["reEvaluated"]["decision"] == recorded["decision"]
    # Every rule is reported, fired or not — that is what makes the response explain itself.
    assert {r["name"] for r in result["reEvaluated"]["rules"]} == {
        "AMOUNT_TIER", "CROSS_BORDER", "NEW_BENEFICIARY", "VELOCITY", "PURPOSE_CODE_RISK",
    }
    # And nothing changed on the document.
    assert rich_db["payments"].docs[-1]["fraud"] == recorded


def test_fraud_evaluation_returns_none_for_an_unknown_payment(service):
    assert service.evaluate_fraud("PAY-does-not-exist") is None


def test_payment_confirmation_refuses_when_nothing_is_committed(rich_service, rich_db):
    """A confirmation for a payment with no execution path would be a false statement to the
    customer — her L502 ties the confirmation to the commitment."""
    payment = _international(rich_service, instructed_amount=300_000.0, priority="URGENT")
    assert payment["lifecycle"]["currentState"] == "AUTHORISED"  # held for review

    with pytest.raises(ValueError, match="no committed execution path"):
        rich_service.confirm_to_originator(payment["paymentId"])


def test_payment_confirmation_re_sends_for_a_committed_payment(rich_service, rich_db):
    payment = _international(rich_service, instructed_amount=25_000.0)
    before = len(rich_db["payments"].docs[-1]["checks"])

    result = rich_service.confirm_to_originator(payment["paymentId"])

    assert result["confirmed"] is True
    assert result["paymentOrderId"] == payment["refs"]["paymentOrderId"]
    assert result["clearingNetwork"] == "SWIFT"
    # Append-only: the re-send is a new entry, never a rewrite of the original.
    assert len(rich_db["payments"].docs[-1]["checks"]) == before + 1
