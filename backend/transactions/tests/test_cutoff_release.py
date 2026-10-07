"""Cutoff release (plan-cutoff-release R1): held `CUTOFF-DEMO-` payments end closed.

Every payment comes from the real scenario starter (`cutoff_scenarios.run_one`) on the seeded
world, so the documents are the production writers' (defect 2026-09-02). Ids are dynamic.

Route coverage: no `TestClient` (`httpx` is not installed), so the route function is called
with a stub request and the status codes are asserted on the `HTTPException`.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api_models import CutoffReleaseRequest
from contexts.payment_order_initiation.domain import lifecycle
from contexts.payment_orchestration.application import (
    cutoff_release,
    cutoff_scenarios,
    cutoff_seed,
)
from contexts.payment_orchestration.domain import cutoff_policy
from routers import workflow
from shared import business_clock
from tests.test_cutoff_scenarios import _seeded_db
from services.payments_service import PaymentsService
from tests.test_payments_service import FakeConnection


@pytest.fixture(autouse=True)
def _fresh_clock_cache(monkeypatch):
    monkeypatch.delenv("ENABLE_CUTOFF_RELEASE_JANITOR", raising=False)
    monkeypatch.delenv("ENABLE_CUTOFF_AGENT", raising=False)
    business_clock._clear_cache()
    yield
    business_clock._clear_cache()


@pytest.fixture
def db():
    return _seeded_db()


@pytest.fixture
def service(db):
    return PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=5_000_000.0)


def _start(service, key):
    return cutoff_scenarios.run_one(service, key)


def _stored(db, started):
    return db["payments"].find_one({"paymentId": started["paymentId"]})


def _release(service, started, **kwargs):
    return cutoff_release.release_run(service, started["runId"], actor="test-actor", **kwargs)


def _only(result):
    assert len(result["payments"]) == 1
    return result["payments"][0]


def _business_date(db, started):
    run = db["demoClocks"].find_one({"_id": started["runId"]})
    return run["businessDate"]


def _next_day(db, started):
    return cutoff_policy.next_business_day(
        datetime.fromisoformat(_business_date(db, started)).date()).isoformat()


# --- plan_for: status x decision x phase ----------------------------------------

def _p(status, decision=None):
    return {"status": status, "cutoff": {"decision": decision} if decision else {}}


@pytest.mark.parametrize("status,decision,phase,action,next_day", [
    ("PENDING_SCREENING", None, cutoff_policy.BEFORE_INTERNAL, "CLEAR_SCREENING", False),
    ("PENDING_SCREENING", None, None, "CLEAR_SCREENING", False),
    ("PENDING_SCREENING", None, cutoff_policy.AFTER_INTERNAL, "CLEAR_SCREENING", True),
    ("PENDING_SCREENING", "HOLD_NEXT_VALUE_DATE", cutoff_policy.BEFORE_INTERNAL,
     "CLEAR_SCREENING", True),
    ("PENDING_FUNDS", None, cutoff_policy.BEFORE_INTERNAL, "CREDIT_AND_RECHECK", True),
    ("PENDING_FUNDS", "HOLD_NEXT_VALUE_DATE", None, "CREDIT_AND_RECHECK", True),
    ("PENDING_APPROVAL", None, cutoff_policy.AFTER_INTERNAL, "APPROVE", True),
    ("CUTOFF_EXCEPTION", None, cutoff_policy.AFTER_INTERNAL, "EXPEDITE", True),
    ("ROUTED", "NEXT_VALUE_DATE", None, "RELEASE", True),
    ("ROUTED", "HOLD_NEXT_VALUE_DATE", None, "RELEASE", True),
    ("ROUTED", None, None, "SKIP_UNKNOWN", False),
    ("ROUTED", "EXPEDITE", None, "SKIP_UNKNOWN", False),
    ("AUTHORISED", None, None, "SKIP_IN_FLIGHT", False),
    ("APPROVED", None, None, "SKIP_IN_FLIGHT", False),
    ("SUBMITTED", "EXPEDITE", None, "SKIP_IN_FLIGHT", False),
    ("IN_PROGRESS", None, None, "SKIP_IN_FLIGHT", False),
    ("MANUAL_FRAUD_REVIEW", None, None, "SKIP_REVIEW", False),
    ("SETTLED", None, None, "SKIP_TERMINAL", False),
    ("RECONCILED", None, None, "SKIP_TERMINAL", False),
    ("REJECTED", None, None, "SKIP_TERMINAL", False),
    ("RETURNED", None, None, "SKIP_TERMINAL", False),
])
def test_plan_for_table(status, decision, phase, action, next_day):
    plan = cutoff_release.plan_for(_p(status, decision), phase=phase)

    assert (plan.action, plan.next_day) == (action, next_day)
    assert plan.skip == action.startswith("SKIP")


# --- end to end, per held state ------------------------------------------------

def test_approval_hold_is_approved_by_maya_and_settles(service, db):
    started = _start(service, "C1")
    assert started["status"] == "PENDING_APPROVAL"

    row = _only(_release(service, started))

    assert row["action"] == "APPROVE"
    assert row["before"] == "PENDING_APPROVAL" and row["after"] in ("IN_PROGRESS", "SETTLED")
    stored = _stored(db, started)
    assert stored["entitlement"]["dualApproval"]["approvedBy"] == cutoff_seed.MAYA
    assert stored["demo"]["release"]["state"] == "DONE"
    assert len(db["transactions"].inserted) == 1


def test_funds_hold_gets_the_credit_once_then_settles(service, db):
    started = _start(service, "C3")
    assert started["status"] == "PENDING_FUNDS"
    before = db["accounts"].find_one({"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT})

    row = _only(_release(service, started))

    after = db["accounts"].find_one({"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT})
    assert row["action"] == "CREDIT_AND_RECHECK"
    assert row["after"] in ("IN_PROGRESS", "SETTLED")
    # +4,000 credit, -wire (6,300 + 3,200): net 800 left, and the credit is on record once.
    assert after["balance"]["available"] == pytest.approx(
        before["balance"]["available"] + 4_000.0 - started["amount"])
    assert [c["paymentId"] for c in after["demoCredits"]] == [started["paymentId"]]
    assert len(db["transactions"].inserted) == 1
    assert after["expectedCredits"] == before["expectedCredits"]


def test_warehoused_deferral_submits_on_the_next_business_day(service, db):
    started = _start(service, "C5")
    assert started["status"] == "CUTOFF_EXCEPTION"
    held = service.decide_cutoff(started["paymentId"], decision="NEXT_VALUE_DATE",
                                 decided_by="OPS-1")
    assert held["status"] == "ROUTED"
    next_day = _next_day(db, started)

    row = _only(_release(service, started))

    stored = _stored(db, started)
    assert row["action"] == "RELEASE" and row["after"] in ("IN_PROGRESS", "SETTLED")
    assert stored["order"]["valueDate"] == next_day
    assert stored["cutoff"]["releasedBy"] == "test-actor"
    assert stored["cutoff"]["decision"] == "NEXT_VALUE_DATE"
    assert len(db["transactions"].inserted) == 1


@pytest.mark.parametrize("key, advance_minutes", [("C1", None), ("C2", 30)])
def test_undecided_release_after_fast_forward_gets_the_next_business_day_value_date(
        service, db, key, advance_minutes):
    # Approver-out (C1) and screening held past the cut-off (C2) carry no HOLD/NEXT_VALUE_DATE
    # decision, so nothing overrides the value date; it must not stay at the capture-time date.
    started = _start(service, key)
    if advance_minutes:
        cutoff_scenarios.move_clock(db, started["runId"], advance_minutes=advance_minutes)
    assert (_stored(db, started).get("cutoff") or {}).get("decision") is None
    next_day = _next_day(db, started)

    row = _only(_release(service, started))

    stored = _stored(db, started)
    assert row["after"] in ("IN_PROGRESS", "SETTLED")
    assert stored["order"]["valueDate"] == next_day
    assert (stored.get("cutoff") or {}).get("decision") is None


def test_in_time_screening_clears_the_same_day_without_moving_the_clock(service, db):
    started = _start(service, "C4")
    assert started["status"] == "PENDING_SCREENING"
    offset_before = db["demoClocks"].find_one({"_id": started["runId"]})["offsetSeconds"]

    row = _only(_release(service, started))

    run = db["demoClocks"].find_one({"_id": started["runId"]})
    assert row["action"] == "CLEAR_SCREENING" and row["after"] in ("IN_PROGRESS", "SETTLED")
    assert run["offsetSeconds"] == offset_before
    assert cutoff_scenarios.FAST_FORWARDED_TO not in run
    assert _stored(db, started)["screening"]["resolvedBy"] == cutoff_seed.STAFF_ANALYST_3


def test_late_screening_clears_after_the_clock_moves(service, db):
    started = _start(service, "C2")
    assert started["status"] == "PENDING_SCREENING"
    cutoff_scenarios.move_clock(db, started["runId"], advance_minutes=30)    # 18:20 ET
    next_day = _next_day(db, started)

    result = _release(service, started)

    row = _only(result)
    # Past the internal cut-off today, in time at the next open, so one clear is enough.
    assert row["action"] == "CLEAR_SCREENING"
    assert row["after"] in ("IN_PROGRESS", "SETTLED")
    assert db["demoClocks"].find_one({"_id": started["runId"]})[
        cutoff_scenarios.FAST_FORWARDED_TO] == next_day
    assert f"{next_day}T09:00:00" in result["clock"]["businessNowEt"]


def test_a_hold_on_screening_chains_through_routed_to_release(service, db):
    started = _start(service, "C4")
    service.hold_next_value_date(started["paymentId"], decided_by="OPS-1", reason="Review late")
    next_day = _next_day(db, started)

    row = _only(_release(service, started))

    stored = _stored(db, started)
    assert row["action"] == "CLEAR_SCREENING+RELEASE"
    assert row["after"] in ("IN_PROGRESS", "SETTLED")
    assert stored["order"]["valueDate"] == next_day
    assert stored["cutoff"]["decision"] == "HOLD_NEXT_VALUE_DATE"


def test_an_open_exception_blocks_the_expedite_and_leaves_the_payment(service, db):
    started = _start(service, "C5")
    db["exceptions"].insert_one({"paymentId": started["paymentId"], "status": "OPEN"})

    row = _only(_release(service, started))

    assert row["after"] == "CUTOFF_EXCEPTION"
    assert "OPEN exception" in row["error"]
    assert _stored(db, started)["demo"]["release"]["state"] == "FAILED"
    assert db["transactions"].inserted == []


# --- claim, idempotence, scope ---------------------------------------------------

def test_a_live_claim_elsewhere_blocks_the_release(service, db):
    started = _start(service, "C3")
    db["payments"].update_one({"paymentId": started["paymentId"]}, {"$set": {"demo.release": {
        "state": "CLAIMED", "by": "other-pod", "at": datetime.now(timezone.utc),
        "leaseUntil": datetime.now(timezone.utc) + timedelta(minutes=2), "attempts": 1}}})

    with pytest.raises(cutoff_release.ReleaseBusy):
        _release(service, started)

    assert _stored(db, started)["status"] == "PENDING_FUNDS"
    assert _stored(db, started)["demo"]["release"]["by"] == "other-pod"


def test_an_expired_claim_can_be_taken_over(service, db):
    started = _start(service, "C3")
    db["payments"].update_one({"paymentId": started["paymentId"]}, {"$set": {"demo.release": {
        "state": "CLAIMED", "by": "dead-pod", "at": datetime.now(timezone.utc),
        "leaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1), "attempts": 1}}})

    row = _only(_release(service, started))

    assert row["after"] in ("IN_PROGRESS", "SETTLED")
    assert _stored(db, started)["demo"]["release"]["attempts"] == 2


def test_attempts_are_capped(service, db):
    started = _start(service, "C3")
    db["payments"].update_one({"paymentId": started["paymentId"]}, {"$set": {"demo.release": {
        "state": "FAILED", "by": "x", "at": datetime.now(timezone.utc),
        "leaseUntil": datetime.now(timezone.utc), "attempts": cutoff_release.MAX_ATTEMPTS}}})

    row = _only(_release(service, started))

    assert row["action"] == "ATTEMPTS_EXHAUSTED"
    assert _stored(db, started)["status"] == "PENDING_FUNDS"


def test_a_second_click_moves_no_more_money(service, db):
    started = _start(service, "C3")
    _release(service, started)
    balance = db["accounts"].find_one({"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT})["balance"]

    again = _release(service, started)

    assert _only(again)["action"] in ("SKIP_IN_FLIGHT", "SKIP_TERMINAL")
    assert db["accounts"].find_one(
        {"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT})["balance"] == balance
    assert len(db["transactions"].inserted) == 1
    assert len(db["accounts"].find_one(
        {"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT})["demoCredits"]) == 1


def test_a_look_alike_without_the_demo_reference_is_never_touched(service, db):
    started = _start(service, "C3")
    db["payments"].update_one({"paymentId": started["paymentId"]},
                              {"$set": {"clientReference": f"REAL-{uuid4().hex[:8]}"}})

    result = _release(service, started)

    assert result["payments"] == []
    assert _stored(db, started)["status"] == "PENDING_FUNDS"
    assert "release" not in _stored(db, started)["demo"]


def test_an_untagged_payment_is_not_found_by_the_run(service, db):
    started = _start(service, "C3")
    db["payments"].update_one({"paymentId": started["paymentId"]},
                              {"$set": {"demo.clockRunId": f"CLK-{uuid4().hex[:8]}"}})

    assert _release(service, started)["payments"] == []
    assert _stored(db, started)["status"] == "PENDING_FUNDS"


def test_release_never_rewrites_the_recorded_decision(service, db):
    started = _start(service, "C5")
    service.decide_cutoff(started["paymentId"], decision="NEXT_VALUE_DATE", decided_by="OPS-1")
    before = _stored(db, started)["cutoff"]

    _release(service, started)

    after = _stored(db, started)["cutoff"]
    for key in ("decision", "decidedBy", "decidedAt", "valueDate"):
        assert after[key] == before[key]


def test_unknown_run_is_a_lookup_error(service):
    with pytest.raises(LookupError):
        cutoff_release.release_run(service, f"CLK-{uuid4().hex}", actor="test-actor")


# --- the funds credit ------------------------------------------------------------

def test_credit_is_applied_once_per_payment(service, db):
    payment = _stored(db, _start(service, "C3"))

    first = cutoff_release.credit_expected_funds(service, payment)
    second = cutoff_release.credit_expected_funds(service, payment)

    account = db["accounts"].find_one({"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT})
    assert (first, second) == (4_000.0, 0.0)
    assert account["balance"]["available"] == cutoff_seed.FUNDS_SHORT_AVAILABLE + 4_000.0
    assert account["balance"]["current"] == account["balance"]["ledger"] == \
        account["balance"]["available"]


def test_credit_refuses_an_account_the_seed_does_not_own(service, db):
    payment = _stored(db, _start(service, "C3"))
    db["accounts"].update_one({"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT},
                              {"$set": {"sourceSystem": "someone-else"}})

    with pytest.raises(ValueError, match="not owned by the cutoff demo seed"):
        cutoff_release.credit_expected_funds(service, payment)

    account = db["accounts"].find_one({"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT})
    assert account["balance"]["available"] == cutoff_seed.FUNDS_SHORT_AVAILABLE
    assert "demoCredits" not in account


# --- the janitor's sweep ---------------------------------------------------------

def _age(db, started, seconds):
    db["demoClocks"].update_one(
        {"_id": started["runId"]},
        {"$set": {"createdAt": datetime.now(timezone.utc) - timedelta(seconds=seconds)}})


def test_sweep_leaves_a_young_run_alone(service, db):
    started = _start(service, "C3")

    assert cutoff_release.sweep_stale(service, older_than_seconds=1200) == []
    assert _stored(db, started)["status"] == "PENDING_FUNDS"


def test_sweep_releases_an_old_run(service, db):
    started = _start(service, "C3")
    _age(db, started, 1300)

    results = cutoff_release.sweep_stale(service, older_than_seconds=1200)

    assert [r["runId"] for r in results] == [started["runId"]]
    assert _stored(db, started)["status"] in ("IN_PROGRESS", "SETTLED")
    assert _stored(db, started)["demo"]["release"]["by"] == cutoff_release.JANITOR_ACTOR


def test_sweep_skips_a_payment_without_the_demo_reference(service, db):
    started = _start(service, "C3")
    _age(db, started, 1300)
    db["payments"].update_one({"paymentId": started["paymentId"]},
                              {"$set": {"clientReference": "REAL-1"}})

    assert cutoff_release.sweep_stale(service, older_than_seconds=1200) == []
    assert _stored(db, started)["status"] == "PENDING_FUNDS"


def test_sweep_recovers_a_run_the_ttl_deleted(service, db):
    started = _start(service, "C3")
    service.hold_next_value_date(started["paymentId"], decided_by="OPS-1", reason="Funds late")
    db["demoClocks"].docs[:] = [d for d in db["demoClocks"].docs
                                if d["_id"] != started["runId"]]

    results = cutoff_release.sweep_stale(service, older_than_seconds=1200)

    stored = _stored(db, started)
    assert len(results) == 1 and results[0]["runId"] != started["runId"]
    assert stored["demo"]["recoveredFrom"] == started["runId"]
    assert stored["demo"]["clockRunId"] == results[0]["runId"]
    assert stored["status"] in ("IN_PROGRESS", "SETTLED")
    assert stored["order"]["valueDate"] == cutoff_policy.next_business_day(
        cutoff_seed.business_date_for(datetime.now(timezone.utc))).isoformat()


def test_recover_run_returns_a_live_run_unchanged(service, db):
    started = _start(service, "C3")

    assert cutoff_release.recover_run(service, started["runId"]) == started["runId"]
    assert _stored(db, started)["demo"]["clockRunId"] == started["runId"]


# --- the route -------------------------------------------------------------------

def _request(service):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(payments_service=service)))


def test_route_returns_the_results(service, db):
    started = _start(service, "C3")

    response = workflow.release_cutoff_run(CutoffReleaseRequest(runId=started["runId"]),
                                           _request(service))

    assert response.status_code == 200
    assert b"CREDIT_AND_RECHECK" in response.body


def test_route_404s_an_unknown_run(service):
    with pytest.raises(HTTPException) as err:
        workflow.release_cutoff_run(CutoffReleaseRequest(runId=f"CLK-{uuid4().hex}"),
                                    _request(service))
    assert err.value.status_code == 404


def test_route_409s_a_run_claimed_elsewhere(service, db):
    started = _start(service, "C3")
    db["payments"].update_one({"paymentId": started["paymentId"]}, {"$set": {"demo.release": {
        "state": "CLAIMED", "by": "other-pod", "at": datetime.now(timezone.utc),
        "leaseUntil": datetime.now(timezone.utc) + timedelta(minutes=2), "attempts": 1}}})

    with pytest.raises(HTTPException) as err:
        workflow.release_cutoff_run(CutoffReleaseRequest(runId=started["runId"]),
                                    _request(service))
    assert err.value.status_code == 409


@pytest.mark.parametrize("body", [{}, {"runId": ""}, {"runId": "CLK-1", "extra": 1}])
def test_request_model_validation(body):
    with pytest.raises(ValidationError):
        CutoffReleaseRequest(**body)


# --- the janitor worker's flag ---------------------------------------------------

def test_janitor_is_off_unless_the_flag_is_true(monkeypatch):
    from workers import cutoff_release_worker

    # `main.py` starts the thread only when `enabled()`; the worker has no disabled branch.
    assert cutoff_release_worker.enabled() is False
    monkeypatch.setenv("ENABLE_CUTOFF_RELEASE_JANITOR", "false")
    assert cutoff_release_worker.enabled() is False
    monkeypatch.setenv("ENABLE_CUTOFF_RELEASE_JANITOR", "true")
    assert cutoff_release_worker.enabled() is True


def test_janitor_release_age_defaults_and_overrides(monkeypatch):
    from workers import cutoff_release_worker

    monkeypatch.delenv("CUTOFF_RELEASE_AFTER_SECONDS", raising=False)
    assert cutoff_release_worker.release_after_seconds() == 1200
    monkeypatch.setenv("CUTOFF_RELEASE_AFTER_SECONDS", "300")
    assert cutoff_release_worker.release_after_seconds() == 300


def test_janitor_cycle_releases_an_old_run(service, db):
    from workers import cutoff_release_worker

    started = _start(service, "C3")
    _age(db, started, 1300)

    results = cutoff_release_worker.sweep_cycle(service, 1200)

    assert [r["runId"] for r in results] == [started["runId"]]


# --- the cleanup script ----------------------------------------------------------

def _expire(db, started):
    db["demoClocks"].docs[:] = [d for d in db["demoClocks"].docs if d["_id"] != started["runId"]]


def test_cleanup_discovers_open_demo_payments_and_skips_the_rest(service, db):
    from data import release_cutoff_stranded as cleanup

    stranded = _start(service, "C3")
    closed = _start(service, "C1")
    db["payments"].update_one({"paymentId": closed["paymentId"]}, {"$set": {"status": "SETTLED"}})
    lookalike = _start(service, "C2")
    db["payments"].update_one({"paymentId": lookalike["paymentId"]},
                              {"$set": {"clientReference": "REAL-1"}})

    rows = cleanup.plan_cleanup(service)

    assert [r.payment_id for r in rows] == [stranded["paymentId"]]
    assert (rows[0].scenario, rows[0].status, rows[0].run_alive, rows[0].action) == (
        "C3", "PENDING_FUNDS", True, "CREDIT_AND_RECHECK")


def test_cleanup_plans_a_recovery_for_an_expired_run(service, db):
    from data import release_cutoff_stranded as cleanup

    started = _start(service, "C3")
    _expire(db, started)

    (row,) = cleanup.plan_cleanup(service)

    assert row.run_alive is False
    assert row.action == "RECOVER_RUN+CREDIT_AND_RECHECK"
    assert "expired" in cleanup.format_table([row])


def test_cleanup_flags_a_missing_or_ineligible_named_id(service, db):
    from data import release_cutoff_stranded as cleanup

    plain = _start(service, "C3")
    db["payments"].update_one({"paymentId": plain["paymentId"]},
                              {"$set": {"clientReference": "REAL-1"}})
    missing = f"PAY-{uuid4().hex[:8]}"

    rows = cleanup.plan_cleanup(service, [missing, plain["paymentId"]])

    assert [r.problem for r in rows] == ["not found", "not a CUTOFF-DEMO payment on a clock run"]
    assert cleanup.run(service, apply=False, payment_ids=[missing]) == 1


def test_cleanup_dry_run_changes_nothing(service, db, capsys):
    from data import release_cutoff_stranded as cleanup

    started = _start(service, "C3")

    assert cleanup.run(service, apply=False) == 0

    assert _stored(db, started)["status"] == "PENDING_FUNDS"
    assert "demo" in _stored(db, started) and "release" not in _stored(db, started)["demo"]
    assert started["paymentId"] in capsys.readouterr().out


def test_cleanup_apply_releases_with_its_own_actor(service, db):
    from data import release_cutoff_stranded as cleanup

    live = _start(service, "C3")
    expired = _start(service, "C1")
    _expire(db, expired)
    business_clock._clear_cache()

    code = cleanup.run(service, apply=True)

    assert _stored(db, live)["demo"]["release"]["by"] == "cutoff-cleanup"
    assert _stored(db, expired)["demo"]["recoveredFrom"] == expired["runId"]
    assert _stored(db, expired)["demo"]["release"]["by"] == "cutoff-cleanup"
    # IN_PROGRESS counts as open (it settles ~30 s later), so the exit code follows status.
    still_open = [p for p in (live, expired)
                  if _stored(db, p)["status"] not in cutoff_release._TERMINAL]
    assert code == (1 if still_open else 0)
