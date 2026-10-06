"""Cutoff plan A2: the four demo-clock holds and their five decisions.

Every test drives the real `initiate_payment` with a `clock_run_id` seeded in `demoClocks`.
Business time comes from the run's `offsetSeconds` — moved by rewriting the run and clearing
the clock cache, never by patching the value a stage decides on.

Route coverage: `httpx` is not installed, so there is no `TestClient` in this suite. The
routes are thin wrappers (`main._run_hold_decision`): `PaymentNotFound` → 404, `ValueError` →
400. These tests pin the service methods that raise those, plus the request models.
"""

import copy
import math
import os
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from contexts.fraud_evaluation.domain import sanctions
from contexts.payment_order_initiation.domain import lifecycle
from contexts.payment_orchestration.domain import cutoff_policy
from services.payments_service import PaymentNotFound, PaymentsService
from shared import business_clock
from tests.test_payment_document_spec import _KNOWN_DEMO_EXTRAS, _KNOWN_EXTRAS, _schema
from tests.test_payments_service import (
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
)

APPROVER = "CUST-0000000003"
CANADIAN_BANK = dict(EXTERNAL_CREDITOR, bic="ROYCCAT2", bankName="Royal Bank of Canada",
                     bankCountry="CA")


def _db(*, available=10_000_000.0):
    """A COMMERCIAL debtor (dual approval above 10,000) with a second signatory."""
    signatories = [
        {"customerId": CUST_D, "type": "PRIMARY", "signingRule": "JOINT",
         "addedAt": "2024-12-07"},
        {"customerId": APPROVER, "type": "SECONDARY", "signingRule": "JOINT",
         "addedAt": "2024-12-07"},
    ]
    return FakeDb({
        "accounts": FakeCollection([
            _account(DEBTOR, CUST_D, available=available, signatories=signatories),
            _account(CREDITOR, CUST_C),
            _CLEARING_WIRE,
        ]),
        "customers": FakeCollection(
            [_customer(CUST_D, segment="COMMERCIAL"), _customer(CUST_C),
             _customer(APPROVER)],
            key="customerId",
        ),
        "payments": FakeCollection(key="paymentId"),
        "transactions": FakeCollection(key="transactionId"),
        "notifications": FakeCollection(key="notificationId"),
        "demoClocks": FakeCollection(key="_id"),
    })


@pytest.fixture
def db():
    return _db()


@pytest.fixture
def service(db):
    return PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=5_000_000.0)


# --- clock helpers: business time via the run's offset ------------------------

def _offset_for(target_et: datetime) -> int:
    return math.ceil((target_et - datetime.now(timezone.utc)).total_seconds())


def _run_at(db, hh: int, mm: int) -> str:
    offset = business_clock.anchor_offset(hh * 60 + mm, real_now=datetime.now(timezone.utc))
    return business_clock.create_run(db["demoClocks"], offset_seconds=offset)


def _move_run(db, run_id: str, hh: int, mm: int) -> None:
    offset = business_clock.anchor_offset(hh * 60 + mm, real_now=datetime.now(timezone.utc))
    db["demoClocks"].update_one({"_id": run_id}, {"$set": {"offsetSeconds": offset}})
    business_clock._clear_cache()


def _next_friday_et(hh: int, mm: int) -> datetime:
    today = business_clock.to_et(datetime.now(timezone.utc)).date()
    friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
    return datetime(friday.year, friday.month, friday.day, hh, mm, tzinfo=business_clock.ET)


def _today_et():
    return business_clock.to_et(datetime.now(timezone.utc)).date()


# --- initiation helpers -------------------------------------------------------

def _domestic(svc, run_id=None, **over):
    kwargs = dict(creditor_account_ref=None, creditor_party=EXTERNAL_CREDITOR,
                  payment_rail="WIRE", clock_run_id=run_id)
    kwargs.update(over)
    return _initiate(svc, **kwargs)


def _international(svc, run_id=None, **over):
    return _domestic(svc, run_id, creditor_party=CANADIAN_BANK, **over)


def _names(payment, result=None):
    return [c["name"] for c in payment.get("checks", [])
            if result is None or c["result"] == result]


def _stored(db, payment):
    return db["payments"].find_one({"paymentId": payment["paymentId"]})


def _assert_demo_extras_only(doc):
    extras = set(doc) - set(_schema()["properties"]) - _KNOWN_EXTRAS
    assert extras <= _KNOWN_DEMO_EXTRAS, extras


# --- lifecycle edges ----------------------------------------------------------

@pytest.mark.parametrize("src,dst", [
    ("INITIATED", "PENDING_APPROVAL"), ("INITIATED", "PENDING_FUNDS"),
    ("PENDING_APPROVAL", "VALIDATED"), ("PENDING_APPROVAL", "PENDING_FUNDS"),
    ("PENDING_FUNDS", "VALIDATED"),
    ("FINAL_VALIDATED", "CUTOFF_EXCEPTION"), ("CUTOFF_EXCEPTION", "ROUTED"),
    ("ROUTED", "PENDING_SCREENING"), ("PENDING_SCREENING", "AUTHORISED"),
    ("PENDING_SCREENING", "MANUAL_FRAUD_REVIEW"), ("PENDING_SCREENING", "CUTOFF_EXCEPTION"),
    ("PENDING_SCREENING", "ROUTED"),
])
def test_new_edges_are_legal(src, dst):
    assert dst in lifecycle.TRANSITIONS[src]


@pytest.mark.parametrize("src,dst", [
    ("CUTOFF_EXCEPTION", "AUTHORISED"), ("PENDING_FUNDS", "PENDING_APPROVAL"),
    ("PENDING_APPROVAL", "ROUTED"), ("PENDING_SCREENING", "SUBMITTED"),
    ("VALIDATED", "PENDING_FUNDS"),
])
def test_unlisted_edges_stay_illegal(src, dst):
    assert dst not in lifecycle.TRANSITIONS[src]


@pytest.mark.parametrize("state", [
    "PENDING_APPROVAL", "PENDING_FUNDS", "CUTOFF_EXCEPTION", "PENDING_SCREENING",
])
def test_the_holds_have_only_pre_execution_terminals(state):
    allowed = lifecycle.TRANSITIONS[state]
    assert {"REJECTED", "CANCELLED", "FAILED"} <= allowed
    assert not {"RETURNED", "REVERSED", "REFUNDED"} & allowed


def test_the_happy_path_is_untouched():
    assert not {"PENDING_APPROVAL", "PENDING_FUNDS", "CUTOFF_EXCEPTION",
                "PENDING_SCREENING"} & set(lifecycle.HAPPY_PATH)


# --- D1 approval --------------------------------------------------------------

def test_tagged_dual_approval_holds_at_pending_approval(service, db):
    payment = _domestic(service, _run_at(db, 10, 0), instructed_amount=25_000.0)

    assert payment["status"] == "PENDING_APPROVAL"
    dual = next(c for c in payment["checks"] if c["name"] == "dual_approval")
    assert dual["result"] == "WARN"
    entitlement = payment["entitlement"]
    assert entitlement["dualApprovalSimulated"] is False
    assert entitlement["dualApproval"]["status"] == "PENDING"
    assert entitlement["dualApproval"]["approvedBy"] is None
    assert "3 validate" not in {c["stage"] for c in payment["checks"]}
    assert db["transactions"].inserted == []
    _assert_demo_extras_only(_stored(db, payment))


def test_approve_by_second_signatory_resumes_to_settled(service, db):
    held = _domestic(service, _run_at(db, 10, 0), instructed_amount=25_000.0)

    resumed = service.approve_payment(held["paymentId"], approver_id=APPROVER,
                                      decision="APPROVED")

    # An external wire's settlement is stage 7's later trigger, so the saga ends at
    # IN_PROGRESS with the money moved to clearing — the same end state as the untagged wire.
    assert resumed["status"] == "IN_PROGRESS"
    assert len(db["transactions"].inserted) == 1
    assert resumed["entitlement"]["dualApproval"]["approvedBy"] == APPROVER
    assert resumed["entitlement"]["dualApprovalSimulated"] is False
    approvals = [c for c in resumed["checks"] if c["name"] == "dual_approval"]
    assert [c["result"] for c in approvals] == ["WARN", "PASS"]
    assert approvals[-1]["actor"] == APPROVER
    # R3: re-running validation must not flag the payment as a duplicate of itself.
    assert resumed["idempotency"]["duplicateOf"] is None
    duplicates = [c for c in resumed["checks"] if c["name"] == "duplicate_detection"]
    assert [c["result"] for c in duplicates] == ["PASS"]
    states = [e["state"] for e in resumed["lifecycle"]["events"]]
    assert states[:4] == ["DRAFT", "INITIATED", "PENDING_APPROVAL", "VALIDATED"]


def test_approve_refuses_initiator_and_non_signatory(service, db):
    held = _domestic(service, _run_at(db, 10, 0), instructed_amount=25_000.0)

    with pytest.raises(ValueError, match="initiator"):
        service.approve_payment(held["paymentId"], approver_id=CUST_D, decision="APPROVED")
    with pytest.raises(ValueError, match="not a signatory"):
        service.approve_payment(held["paymentId"], approver_id=CUST_C, decision="APPROVED")
    assert _stored(db, held)["status"] == "PENDING_APPROVAL"


def test_approve_rejected_terminates(service, db):
    held = _domestic(service, _run_at(db, 10, 0), instructed_amount=25_000.0)

    rejected = service.approve_payment(held["paymentId"], approver_id=APPROVER,
                                       decision="REJECTED")

    assert rejected["status"] == "REJECTED"
    assert rejected["lifecycle"]["events"][-1]["actor"] == APPROVER
    assert db["transactions"].inserted == []


def test_untagged_dual_approval_still_simulated(service, db):
    payment = _domestic(service, instructed_amount=25_000.0)

    assert payment["status"] == "IN_PROGRESS"
    assert payment["entitlement"]["dualApprovalSimulated"] is True
    assert payment["entitlement"]["dualApprovalBy"] == "SIMULATED-APPROVER-OPS"
    assert "demo" not in payment and "cutoff" not in payment


# --- D2 funds -----------------------------------------------------------------

@pytest.fixture
def short_db():
    return _db(available=1_000.0)


@pytest.fixture
def short_service(short_db):
    return PaymentsService(FakeConnection(short_db), "leafy_bank_bian",
                           payment_limit_usd=5_000_000.0)


def test_tagged_short_wire_holds_at_pending_funds(short_service, short_db):
    payment = _domestic(short_service, _run_at(short_db, 10, 0), instructed_amount=5_000.0)

    assert payment["status"] == "PENDING_FUNDS"
    funds = next(c for c in payment["checks"] if c["name"] == "funds_available")
    assert (funds["result"], funds["code"]) == ("WARN", "INSUFFICIENT_FUNDS_HELD")
    assert payment["validation"]["overallStatus"] == "PASSED"
    assert short_db["transactions"].inserted == []


def test_funds_recheck_still_short_stays_pending(short_service, short_db):
    held = _domestic(short_service, _run_at(short_db, 10, 0), instructed_amount=5_000.0)

    payment, short_by = short_service.recheck_funds(held["paymentId"])

    assert short_by == 4_000.0
    assert payment["status"] == "PENDING_FUNDS"
    assert payment["lifecycle"]["events"] == _stored(short_db, held)["lifecycle"]["events"]


def _credit(db, amount):
    db["accounts"].update_one({"accountId": DEBTOR},
                              {"$set": {"balance.available": amount, "balance.current": amount}})


def test_funds_recheck_after_credit_resumes(short_service, short_db):
    held = _domestic(short_service, _run_at(short_db, 10, 0), instructed_amount=5_000.0)
    _credit(short_db, 20_000.0)

    payment, short_by = short_service.recheck_funds(held["paymentId"])

    assert short_by is None
    assert payment["status"] == "IN_PROGRESS"
    states = [e["state"] for e in payment["lifecycle"]["events"]]
    assert states[2:4] == ["PENDING_FUNDS", "VALIDATED"]
    assert payment["idempotency"]["duplicateOf"] is None


def test_tagged_short_internal_transfer_still_rejects(short_service, short_db):
    with pytest.raises(ValueError, match="Insufficient available balance"):
        _initiate(short_service, instructed_amount=5_000.0,
                  clock_run_id=_run_at(short_db, 10, 0))
    assert short_db["payments"].docs[-1]["status"] == "REJECTED"


def test_untagged_short_wire_still_rejects(short_service, short_db):
    with pytest.raises(ValueError, match="Insufficient available balance"):
        _domestic(short_service, instructed_amount=5_000.0)
    assert short_db["payments"].docs[-1]["status"] == "REJECTED"


# --- D4 screening -------------------------------------------------------------

POTENTIAL = dict(EXTERNAL_CREDITOR, name="Northgate Trading FZE")


def test_potential_match_rule_is_deterministic():
    for name in ("Northgate Trading FZE", "  severn maritime llc "):
        outcome = sanctions.screen(creditor_name=name, creditor_country="US")
        assert outcome.status == sanctions.PENDING
        assert not outcome.refuses
    assert sanctions.screen(creditor_name="Northgate Trading", creditor_country="US").status \
        == sanctions.CLEAR
    assert sanctions.potential_matches() == {"NORTHGATE TRADING FZE", "SEVERN MARITIME LLC"}


def test_potential_match_names_unused_by_sims():
    """No simulator, autofill or recon scenario may carry a potential-match name, or untagged
    traffic would start WARNing on screening."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    skip_dirs = {"node_modules", ".venv", ".git", ".next", "__pycache__", "tests", ".claude"}
    needles = [n.lower() for n in sanctions.potential_matches()]
    offenders = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            if not name.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".json")):
                continue
            path = os.path.join(dirpath, name)
            if path.endswith(os.path.join("domain", "sanctions.py")):
                continue
            try:
                text = open(path, encoding="utf-8", errors="ignore").read().lower()
            except OSError:
                continue
            offenders += [path for n in needles if n in text]
    assert offenders == []


def test_tagged_potential_match_holds_at_pending_screening(service, db):
    payment = _domestic(service, _run_at(db, 10, 0), creditor_party=POTENTIAL)

    assert payment["status"] == "PENDING_SCREENING"
    assert payment["screening"]["status"] == "PENDING"
    assert payment["screening"]["matched"] == "NORTHGATE TRADING FZE"
    assert payment["correspondent"]["sanctionsCheck"]["status"] == "PENDING"
    assert "risk_assessment" not in _names(payment)
    assert payment["order"] is None
    _assert_demo_extras_only(_stored(db, payment))


def test_screening_clear_before_cutoff_resumes_and_rescores(service, db):
    held = _domestic(service, _run_at(db, 10, 0), creditor_party=POTENTIAL)

    resumed = service.resolve_screening(held["paymentId"], analyst_id="ANALYST-1",
                                        outcome="CLEAR")

    assert resumed["status"] == "IN_PROGRESS"
    assert _names(resumed).count("risk_assessment") == 1
    screens = [c for c in resumed["checks"] if c["name"] == "sanctions_screening"]
    assert [(c["result"], c["actor"]) for c in screens] == [
        ("WARN", "fraud-service"), ("PASS", "ANALYST-1"),
    ]
    assert resumed["screening"]["outcome"] == "CLEAR"
    assert resumed["screening"]["resolvedBy"] == "ANALYST-1"
    assert resumed["correspondent"]["sanctionsCheck"]["status"] == "CLEAR"
    assert resumed["order"]["valueDate"] is not None


def test_screening_hit_rejects(service, db):
    held = _domestic(service, _run_at(db, 10, 0), creditor_party=POTENTIAL)

    rejected = service.resolve_screening(held["paymentId"], analyst_id="ANALYST-1",
                                         outcome="HIT")

    assert rejected["status"] == "REJECTED"
    assert rejected["screening"]["outcome"] == "HIT"
    assert rejected["correspondent"]["sanctionsCheck"]["status"] == "HIT"
    assert db["transactions"].inserted == []


def test_screening_clear_after_internal_cutoff_diverts_to_cutoff_exception(service, db):
    run_id = _run_at(db, 17, 0)
    held = _domestic(service, run_id, creditor_party=POTENTIAL)
    assert held["status"] == "PENDING_SCREENING"
    _move_run(db, run_id, 17, 40)

    diverted = service.resolve_screening(held["paymentId"], analyst_id="ANALYST-1",
                                         outcome="CLEAR")

    assert diverted["status"] == "CUTOFF_EXCEPTION"
    assert diverted["cutoff"]["resumeFrom"] == "4b"
    assert diverted["screening"]["outcome"] == "CLEAR"

    expedited = service.decide_cutoff(held["paymentId"], decision="EXPEDITE",
                                      decided_by="OPS-1")

    assert expedited["status"] == "IN_PROGRESS"
    states = [e["state"] for e in expedited["lifecycle"]["events"]]
    assert states.count("PENDING_SCREENING") == 1, "a cleared name must not hold again"
    assert _names(expedited).count("risk_assessment") == 1


def test_untagged_potential_match_warns_and_continues(service, db):
    payment = _domestic(service, creditor_party=POTENTIAL)

    assert payment["status"] == "IN_PROGRESS"
    screen = next(c for c in payment["checks"] if c["name"] == "sanctions_screening")
    assert screen["result"] == "WARN"
    assert "screening" not in payment


# --- D3 cutoff ----------------------------------------------------------------

@pytest.mark.parametrize("initiate,before,after", [
    (_domestic, (17, 29), (17, 31)),
    (_international, (18, 14), (18, 16)),
])
def test_tagged_wire_after_internal_cutoff_holds(service, db, initiate, before, after):
    on_time = initiate(service, _run_at(db, *before))
    late = initiate(service, _run_at(db, *after), instructed_amount=260.0)

    assert on_time["status"] == "IN_PROGRESS"
    assert next(c for c in on_time["checks"] if c["name"] == "cutoff_window")["result"] == "PASS"
    assert on_time["cutoff"]["phaseAtCheck"] == "BEFORE_INTERNAL"

    assert late["status"] == "CUTOFF_EXCEPTION"
    assert next(c for c in late["checks"] if c["name"] == "cutoff_window")["result"] == "WARN"
    assert late["cutoff"]["phaseAtCheck"] == "AFTER_INTERNAL"
    assert late["cutoff"]["resumeFrom"] == "4a"
    assert late["refs"]["routingSnapshotId"], "routing is recorded before the hold"
    assert late["wireDetails"]["network"]
    assert late["order"] is None
    _assert_demo_extras_only(_stored(db, late))


def test_expedite_before_external_resumes_to_settled_with_today_value_date(service, db):
    held = _domestic(service, _run_at(db, 17, 40))

    resumed = service.decide_cutoff(held["paymentId"], decision="EXPEDITE",
                                    decided_by="OPS-1")

    assert resumed["status"] == "IN_PROGRESS"
    assert resumed["cutoff"]["decision"] == "EXPEDITE"
    assert resumed["cutoff"]["valueDate"] == _today_et().isoformat()
    assert resumed["order"]["valueDate"] == _today_et().isoformat()
    assert len(db["transactions"].inserted) == 1


def test_expedite_refused_after_external_cutoff(service, db):
    run_id = _run_at(db, 17, 40)
    held = _domestic(service, run_id)
    _move_run(db, run_id, 18, 50)

    with pytest.raises(ValueError, match="external cut-off"):
        service.decide_cutoff(held["paymentId"], decision="EXPEDITE", decided_by="OPS-1")
    assert _stored(db, held)["status"] == "CUTOFF_EXCEPTION"


def test_expedite_refused_with_an_open_exception(service, db):
    held = _domestic(service, _run_at(db, 17, 40))
    db["exceptions"].insert_one({"paymentId": held["paymentId"], "status": "OPEN"})

    with pytest.raises(ValueError, match="OPEN exception"):
        service.decide_cutoff(held["paymentId"], decision="EXPEDITE", decided_by="OPS-1")


def test_next_value_date_warehouses_at_routed_with_next_business_day(service, db):
    friday = _next_friday_et(18, 20)
    run_id = business_clock.create_run(db["demoClocks"], offset_seconds=_offset_for(friday))
    held = _domestic(service, run_id)
    assert held["status"] == "CUTOFF_EXCEPTION"

    decided = service.decide_cutoff(held["paymentId"], decision="NEXT_VALUE_DATE",
                                    decided_by="OPS-1")

    monday = (friday + timedelta(days=3)).date().isoformat()
    assert decided["status"] == "ROUTED"
    assert decided["cutoff"]["valueDate"] == monday
    assert decided["requestedExecutionDate"] == monday
    assert decided["order"] is None
    assert db["transactions"].inserted == []


def test_cutoff_resume_does_not_rewrite_routing_snapshot(service, db):
    held = _domestic(service, _run_at(db, 17, 40))
    before = copy.deepcopy(db["routingSnapshots"].docs)

    resumed = service.decide_cutoff(held["paymentId"], decision="EXPEDITE",
                                    decided_by="OPS-1")

    assert db["routingSnapshots"].docs == before
    assert len(before) == 1
    assert resumed["refs"]["routingSnapshotId"] == before[0]["routingSnapshotId"]


def test_untagged_late_wire_still_warns_and_settles(service, db):
    """Untagged traffic runs on real time, so the hour is not controllable here; what is
    pinned is that no untagged wire meets the new check or the hold, at any hour."""
    payment = _domestic(service)

    assert payment["status"] == "IN_PROGRESS"
    assert "within_cutoff" in _names(payment)
    assert "cutoff_window" not in _names(payment)
    assert "cutoff" not in payment


# --- cross-cutting ------------------------------------------------------------

def test_approval_released_late_trips_cutoff_exception(service, db):
    """C1: approved after the internal cut-off, the payment meets the cut-off on release."""
    run_id = _run_at(db, 17, 0)
    held = _domestic(service, run_id, instructed_amount=25_000.0)
    assert held["status"] == "PENDING_APPROVAL"
    _move_run(db, run_id, 17, 40)

    released = service.approve_payment(held["paymentId"], approver_id=APPROVER,
                                       decision="APPROVED")

    assert released["status"] == "CUTOFF_EXCEPTION"
    assert released["cutoff"]["resumeFrom"] == "4a"


def test_hold_next_value_date_on_pending_funds(short_service, short_db):
    held = _domestic(short_service, _run_at(short_db, 10, 0), instructed_amount=5_000.0)

    recorded = short_service.hold_next_value_date(held["paymentId"], decided_by="OPS-1",
                                                  reason="Funds expected tomorrow")

    expected = cutoff_policy.next_business_day(_today_et()).isoformat()
    assert recorded["status"] == "PENDING_FUNDS", "a recorded decision, not a transition"
    assert recorded["cutoff"]["decision"] == "HOLD_NEXT_VALUE_DATE"
    assert recorded["requestedExecutionDate"] == expected

    _credit(short_db, 20_000.0)
    released, _ = short_service.recheck_funds(held["paymentId"])

    assert released["status"] == "ROUTED", "released into the warehouse, not executed"
    assert short_db["transactions"].inserted == []


def test_hold_next_value_date_on_pending_screening_warehouses_on_clear(service, db):
    held = _domestic(service, _run_at(db, 10, 0), creditor_party=POTENTIAL)
    service.hold_next_value_date(held["paymentId"], decided_by="OPS-1", reason="Review late")

    released = service.resolve_screening(held["paymentId"], analyst_id="ANALYST-1",
                                         outcome="CLEAR")

    assert released["status"] == "ROUTED"
    assert released["order"] is None
    assert db["transactions"].inserted == []


def test_context_from_doc_restores_clock_and_overrides(service, db):
    run_id = _run_at(db, 17, 40)
    held = _domestic(service, run_id)
    db["payments"].update_one({"paymentId": held["paymentId"]}, {"$set": {
        "screening.outcome": "CLEAR",
        "cutoff.decision": "EXPEDITE",
        "cutoff.valueDate": "2026-10-06",
    }})

    ctx = service._context_from_doc(_stored(db, held), customer_ref=CUST_D,
                                    authentication=None)

    assert ctx.clock_run_id == run_id
    assert business_clock.minutes_since_midnight_et(ctx.now) == 17 * 60 + 40
    assert ctx.screening_override == "CLEAR"
    assert ctx.cutoff_decision == "EXPEDITE"
    assert ctx.value_date_override == "2026-10-06"


def test_routes_refuse_untagged_payments(service, db):
    untagged = _domestic(service)
    pid = untagged["paymentId"]
    decisions = [
        lambda p: service.approve_payment(p, approver_id=APPROVER, decision="APPROVED"),
        lambda p: service.recheck_funds(p),
        lambda p: service.resolve_screening(p, analyst_id="A", outcome="CLEAR"),
        lambda p: service.decide_cutoff(p, decision="EXPEDITE", decided_by="OPS-1"),
        lambda p: service.hold_next_value_date(p, decided_by="OPS-1", reason="r"),
    ]
    for decide in decisions:
        with pytest.raises(ValueError, match="not a demo-clock payment"):
            decide(pid)
        with pytest.raises(PaymentNotFound):
            decide("PAY-missing")
    assert _stored(db, untagged)["status"] == "IN_PROGRESS"


def test_routes_refuse_a_payment_in_the_wrong_hold(service, db):
    held = _domestic(service, _run_at(db, 17, 40))
    with pytest.raises(ValueError, match="not PENDING_APPROVAL"):
        service.approve_payment(held["paymentId"], approver_id=APPROVER, decision="APPROVED")
    with pytest.raises(ValueError, match="not PENDING_APPROVAL or PENDING_FUNDS"):
        service.hold_next_value_date(held["paymentId"], decided_by="OPS-1", reason="r")


@pytest.mark.parametrize("model,body", [
    ("PaymentOrderApproveRequest",
     {"paymentId": "P", "approverId": "C", "decision": "APPROVED"}),
    ("PaymentOrderFundsRecheckRequest", {"paymentId": "P"}),
    ("ScreeningResolveRequest", {"paymentId": "P", "analystId": "A", "outcome": "CLEAR"}),
    ("CutoffDecisionRequest", {"paymentId": "P", "decision": "EXPEDITE", "decidedBy": "O"}),
    ("HoldNextValueDateRequest", {"paymentId": "P", "decidedBy": "O", "reason": "r"}),
])
def test_hold_request_models_forbid_extras(model, body):
    import api_models

    cls = getattr(api_models, model)
    cls(**body)
    with pytest.raises(ValidationError):
        cls(**body, demo={"clockRunId": "CLK-x"})


@pytest.mark.parametrize("model,body", [
    ("PaymentOrderApproveRequest", {"paymentId": "P", "approverId": "C", "decision": "MAYBE"}),
    ("ScreeningResolveRequest", {"paymentId": "P", "analystId": "A", "outcome": "PENDING"}),
    ("CutoffDecisionRequest", {"paymentId": "P", "decision": "WAIT", "decidedBy": "O"}),
])
def test_hold_request_models_reject_unknown_decisions(model, body):
    import api_models

    with pytest.raises(ValidationError):
        getattr(api_models, model)(**body)
