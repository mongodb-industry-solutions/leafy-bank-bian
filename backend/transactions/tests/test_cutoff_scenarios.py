"""Cutoff plan Part B gate: scenarios C1-C5 and the presenter's clock.

The FakeDb is the seeded world — ABC's account and customer straight from the sample files
(as `load_stage2_seed.py` loads them), plus everything `cutoff_seed` builds, joined the way
`load_cutoff_seed.py` joins it. Every scenario starts through the real `initiate_payment`.

Route coverage: no `TestClient` (`httpx` is not installed). The routes are thin wrappers —
`ValueError` → 400, `LookupError` → 404 — so these tests pin `run_one` / `move_clock` and
the request models.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bson.json_util import loads
from pydantic import ValidationError

from contexts.payment_order_initiation.domain import lifecycle
from contexts.payment_orchestration.application import cutoff_scenarios, cutoff_seed
from contexts.payment_orchestration.domain import cutoff_policy
from services.payments_service import PaymentsService
from shared import business_clock, hold_queues
from tests.test_payments_service import (
    FakeCollection,
    FakeConnection,
    FakeDb,
    _CLEARING_WIRE,
)

SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample"
KEYS = list(cutoff_scenarios.SCENARIOS)


def _sample(collection: str, key: str, ref: str) -> dict:
    docs = loads((SAMPLE / f"leafy_bank_bian.{collection}.json").read_text())
    return next(d for d in docs if d.get(key) == ref)


def _seeded_db() -> FakeDb:
    today = cutoff_seed.business_date_for(datetime.now(timezone.utc))
    abc = _sample("accounts", "accountId", cutoff_seed.ABC_ACCOUNT)
    abc["signatories"] += cutoff_seed.ABC_SIGNATORIES
    return FakeDb({
        "accounts": FakeCollection([abc, cutoff_seed.funds_short_account_doc(),
                                    _CLEARING_WIRE]),
        "customers": FakeCollection(
            [_sample("customers", "customerId", cutoff_seed.ABC_OWNER)],
            key="customerId"),
        "payments": FakeCollection(key="paymentId"),
        "transactions": FakeCollection(key="transactionId"),
        "notifications": FakeCollection(key="notificationId"),
        "demoClocks": FakeCollection(key="_id"),
        "staffDirectory": FakeCollection(cutoff_seed.staff_docs(today), key="staffId"),
        "approvalRequests": FakeCollection(cutoff_seed.synthetic_lena_requests(today),
                                           key="approvalRequestId"),
    })


@pytest.fixture(autouse=True)
def _fresh_clock_cache():
    business_clock._clear_cache()
    yield
    business_clock._clear_cache()


@pytest.fixture
def db():
    return _seeded_db()


@pytest.fixture
def service(db):
    return PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=5_000_000.0)


def _stored(db, result):
    return db["payments"].find_one({"paymentId": result["paymentId"]})


def _move(db, run_id, hh, mm):
    return cutoff_scenarios.move_clock(db, run_id, anchor=f"{hh:02d}:{mm:02d}")


def _states(payment):
    return [e["state"] for e in payment["lifecycle"]["events"]]


# --- each scenario reaches its hold -------------------------------------------

@pytest.mark.parametrize("key", KEYS)
def test_each_scenario_reaches_its_hold(service, db, key):
    result = cutoff_scenarios.run_one(service, key)

    expected = cutoff_scenarios.SCENARIOS[key].expected_status
    assert result["status"] == expected
    assert "warning" not in result
    stored = _stored(db, result)
    assert stored["status"] == expected
    assert stored["demo"]["clockRunId"] == result["runId"]
    assert stored["clientReference"] == f"CUTOFF-DEMO-{key}"
    assert db["transactions"].inserted == []


@pytest.mark.parametrize("key", KEYS)
def test_every_scenario_wire_meets_the_fedwire_external_cutoff(service, db, key):
    stored = _stored(db, cutoff_scenarios.run_one(service, key))

    window = cutoff_policy.window_for(rail=stored["rail"],
                                      wire_type=stored["wireDetails"]["wireType"],
                                      currency=stored["instructedCurrency"])
    assert window is not None
    assert window.external_minutes_et == 18 * 60 + 45
    assert window.external_network == "FEDWIRE"


@pytest.mark.parametrize("key", KEYS)
def test_rerunning_a_scenario_supersedes_and_still_holds(service, db, key):
    results = [cutoff_scenarios.run_one(service, key) for _ in range(3)]

    expected = cutoff_scenarios.SCENARIOS[key].expected_status
    assert [r["status"] for r in results] == [expected] * 3
    assert results[1]["superseded"] == [results[0]["paymentId"]]
    assert results[2]["superseded"] == [results[1]["paymentId"]]
    assert [_stored(db, r)["status"] for r in results[:2]] == ["REJECTED", "REJECTED"]
    assert _stored(db, results[0])["lifecycle"]["events"][-1]["reason"] == \
        cutoff_scenarios.SUPERSEDED_REASON
    for name in ("approvalRequests", "screeningQueue"):
        open_real = [d for d in db[name].docs
                     if d["status"] == hold_queues.OPEN and not d.get("synthetic")]
        assert [d["paymentId"] for d in open_real] in ([], [results[2]["paymentId"]])
        cancelled = {d["paymentId"] for d in db[name].docs
                     if d["status"] == hold_queues.CANCELLED and not d.get("synthetic")}
        assert cancelled <= {results[0]["paymentId"], results[1]["paymentId"]}
    stale_backlog = [d for d in db["screeningQueue"].docs
                     if d["status"] == hold_queues.OPEN and d.get("synthetic")
                     and d["demo"]["clockRunId"] != results[2]["runId"]]
    assert stale_backlog == []


# --- the stories ----------------------------------------------------------------

def test_c1_story_runs_to_submitted(service, db):
    result = cutoff_scenarios.run_one(service, "C1")

    request = result["approvalRequest"]
    assert request["primaryApprover"] == cutoff_seed.STAFF_MAYA
    assert {cutoff_seed.MAYA, cutoff_seed.RAJ} <= set(request["requiredApprovers"])
    staff = {s["staffId"]: s for s in db["staffDirectory"].docs}

    clock = _move(db, result["runId"], 18, 16)
    at = clock["businessNow"]
    assert not hold_queues.on_shift(staff[cutoff_seed.STAFF_MAYA], at)
    assert hold_queues.on_shift(staff[cutoff_seed.STAFF_RAJ], at)

    released = service.approve_payment(result["paymentId"], approver_id=cutoff_seed.RAJ,
                                       decision="APPROVED")
    assert released["status"] == lifecycle.CUTOFF_EXCEPTION

    expedited = service.decide_cutoff(result["paymentId"], decision="EXPEDITE",
                                      decided_by="OPS-1")
    assert lifecycle.SUBMITTED in _states(expedited)
    assert expedited["status"] == lifecycle.IN_PROGRESS
    assert len(db["transactions"].inserted) == 1


def test_c2_story_clear_after_internal_cutoff_then_expedite(service, db):
    result = cutoff_scenarios.run_one(service, "C2")

    assert result["screening"]["position"] == 8
    assert result["screening"]["analystsOnShift"] == 1

    _move(db, result["runId"], 18, 20)
    diverted = service.resolve_screening(result["paymentId"], analyst_id="STAFF-analyst-3",
                                         outcome="CLEAR")
    assert diverted["status"] == lifecycle.CUTOFF_EXCEPTION

    expedited = service.decide_cutoff(result["paymentId"], decision="EXPEDITE",
                                      decided_by="OPS-1")
    assert lifecycle.SUBMITTED in _states(expedited)
    assert expedited["status"] == lifecycle.IN_PROGRESS


def test_c3_is_short_by_3200_and_never_writes_the_balance(service, db):
    before = db["accounts"].find_one({"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT})["balance"]

    result = cutoff_scenarios.run_one(service, "C3")

    assert result["amount"] == cutoff_seed.FUNDS_SHORT_AVAILABLE + 3_200
    assert result["shortBy"] == 3_200
    after = db["accounts"].find_one({"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT})["balance"]
    assert after == before


def test_c3_resets_a_drifted_account_to_its_opening_balance(service, db):
    db["accounts"].update_one({"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT},
                              {"$set": {"balance.available": 12_297.89,
                                        "balance.current": 12_297.89,
                                        "balance.ledger": 12_297.89}})

    result = cutoff_scenarios.run_one(service, "C3")

    assert result["amount"] == cutoff_seed.FUNDS_SHORT_AVAILABLE + 3_200
    balance = db["accounts"].find_one({"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT})["balance"]
    assert balance["available"] == balance["current"] == balance["ledger"] == \
        cutoff_seed.FUNDS_SHORT_AVAILABLE


def test_c4_is_four_minutes_in_stage_at_position_two(service, db):
    result = cutoff_scenarios.run_one(service, "C4")

    assert result["screening"]["position"] == 2
    assert 3.9 <= result["minutesInStage"] <= 4.1
    assert business_clock.to_et(result["businessNow"]).strftime("%H:%M") == "16:34"


def test_c5_is_ten_minutes_from_fedwire(service, db):
    result = cutoff_scenarios.run_one(service, "C5")

    assert 9.5 <= result["minutesToExternalCutoff"] <= 10.0


def test_synthetic_queue_items_create_no_payments(service, db):
    cutoff_scenarios.run_one(service, "C2")

    synthetic = [d for d in db["screeningQueue"].docs if d["synthetic"]]
    assert len(synthetic) == 7
    payment_ids = {p["paymentId"] for p in db["payments"].docs}
    assert not payment_ids & {d["paymentId"] for d in synthetic}
    assert len(payment_ids) == 1


def test_unknown_scenario_raises_key_error(service):
    with pytest.raises(KeyError):
        cutoff_scenarios.run_one(service, "C9")


# --- the clock ------------------------------------------------------------------

def test_weekend_rolls_back_to_the_previous_weekday():
    saturday = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    sunday = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)
    friday = cutoff_seed.business_date_for(saturday)

    assert friday.isoformat() == "2026-10-09"
    assert cutoff_seed.business_date_for(sunday) == friday
    offset = business_clock.anchor_offset(17 * 60 + 5, real_now=sunday, on_date=friday)
    local = business_clock.to_et(sunday + timedelta(seconds=offset))
    assert (local.date(), local.hour, local.minute) == (friday, 17, 5)


def test_run_records_its_anchor_and_business_date(service, db):
    result = cutoff_scenarios.run_one(service, "C1")

    run = db["demoClocks"].find_one({"_id": result["runId"]})
    assert run["scenario"] == "C1"
    assert run["anchorEtMinutes"] == 17 * 60 + 5
    assert run["businessDate"] == cutoff_seed.business_date_for(
        datetime.now(timezone.utc)).isoformat()


def test_advance_moves_only_its_run_and_invalidates_the_cache(service, db):
    mine = cutoff_scenarios.run_one(service, "C1")["runId"]
    other = cutoff_scenarios.run_one(service, "C3")["runId"]
    clocks = db["demoClocks"]
    other_before = clocks.find_one({"_id": other})["offsetSeconds"]
    business_clock.now(mine, clocks=clocks)           # warm the cache
    before = clocks.find_one({"_id": mine})["offsetSeconds"]

    moved = cutoff_scenarios.move_clock(db, mine, advance_minutes=30)

    assert moved["offsetSeconds"] == before + 1800
    assert clocks.find_one({"_id": other})["offsetSeconds"] == other_before
    now = business_clock.now(mine, clocks=clocks)
    assert business_clock.minutes_since_midnight_et(now) == 17 * 60 + 35


def test_anchor_refuses_backwards(service, db):
    run_id = cutoff_scenarios.run_one(service, "C1")["runId"]
    _move(db, run_id, 18, 0)

    with pytest.raises(ValueError, match="only moves forward"):
        _move(db, run_id, 17, 30)
    with pytest.raises(ValueError, match="only moves forward"):
        cutoff_scenarios.move_clock(db, run_id, advance_minutes=0)


def test_reset_restores_the_start_minute(service, db):
    run_id = cutoff_scenarios.run_one(service, "C1")["runId"]
    _move(db, run_id, 18, 30)

    clock = cutoff_scenarios.move_clock(db, run_id, reset=True)

    assert business_clock.to_et(clock["businessNow"]).strftime("%H:%M") == "17:05"
    now = business_clock.now(run_id, clocks=db["demoClocks"])
    assert business_clock.minutes_since_midnight_et(now) == 17 * 60 + 5


@pytest.mark.parametrize("kwargs", [
    {}, {"anchor": "18:00", "reset": True}, {"advance_minutes": 5, "anchor": "18:00"},
    {"next_business_day": "09:00", "reset": True},
    {"next_business_day": "09:00", "anchor": "18:00"},
    {"next_business_day": "09:00", "advance_minutes": 5},
])
def test_move_clock_needs_exactly_one_action(service, db, kwargs):
    run_id = cutoff_scenarios.run_one(service, "C1")["runId"]
    with pytest.raises(ValueError, match="exactly one"):
        cutoff_scenarios.move_clock(db, run_id, **kwargs)


def test_next_business_day_jumps_to_the_next_open_and_moves_the_business_date(service, db):
    run_id = cutoff_scenarios.run_one(service, "C1")["runId"]
    run = db["demoClocks"].find_one({"_id": run_id})
    expected = cutoff_policy.next_business_day(
        datetime.fromisoformat(run["businessDate"]).date())

    clock = cutoff_scenarios.move_clock(db, run_id, next_business_day="09:00")

    business_et = business_clock.to_et(clock["businessNow"])
    assert business_et.date() == expected and business_et.strftime("%H:%M") == "09:00"
    stored = db["demoClocks"].find_one({"_id": run_id})
    assert stored["businessDate"] == expected.isoformat()
    now = business_clock.now(run_id, clocks=db["demoClocks"])
    assert business_clock.minutes_since_midnight_et(now) == 9 * 60


def test_next_business_day_from_a_friday_lands_on_monday(service, db):
    run_id = cutoff_scenarios.run_one(service, "C1")["runId"]
    friday = datetime(2026, 10, 9).date()
    db["demoClocks"].update_one({"_id": run_id}, {"$set": {"businessDate": friday.isoformat()}})

    clock = cutoff_scenarios.move_clock(db, run_id, next_business_day="09:00")

    assert business_clock.to_et(clock["businessNow"]).date().isoformat() == "2026-10-12"
    assert db["demoClocks"].find_one({"_id": run_id})["businessDate"] == "2026-10-12"


def test_next_business_day_is_forward_only_and_repeatable(service, db):
    run_id = cutoff_scenarios.run_one(service, "C1")["runId"]
    first = cutoff_scenarios.move_clock(db, run_id, next_business_day="09:00")
    later = datetime.now(timezone.utc) + timedelta(minutes=10)

    again = cutoff_scenarios.move_clock(db, run_id, next_business_day="09:00", real_now=later)

    stored = db["demoClocks"].find_one({"_id": run_id})
    assert again["offsetSeconds"] == first["offsetSeconds"] == stored["offsetSeconds"]
    assert stored["businessDate"] == stored[cutoff_scenarios.FAST_FORWARDED_TO]


def test_demo_clock_request_does_not_expose_next_business_day():
    import api_models

    with pytest.raises(ValidationError):
        api_models.DemoClockRequest(runId="CLK-1", next_business_day="09:00")
    with pytest.raises(ValidationError):
        api_models.DemoClockRequest(runId="CLK-1", nextBusinessDay="09:00")


def test_unknown_run_is_a_lookup_error(db):
    with pytest.raises(LookupError):
        cutoff_scenarios.move_clock(db, "CLK-missing", advance_minutes=5)


# --- request models -------------------------------------------------------------

def test_cutoff_scenario_request_rejects_unknown_keys_and_extras():
    import api_models

    api_models.CutoffScenarioRequest(scenario="C3")
    with pytest.raises(ValidationError):
        api_models.CutoffScenarioRequest(scenario="C9")
    with pytest.raises(ValidationError):
        api_models.CutoffScenarioRequest(scenario="C1", demo={"clockRunId": "CLK-x"})


@pytest.mark.parametrize("body", [
    {"runId": "CLK-x"},
    {"runId": "CLK-x", "anchor": "18:00", "reset": True},
    {"runId": "CLK-x", "anchor": "18:00", "advanceMinutes": 5},
    {"runId": "CLK-x", "anchor": "25:00"},
    {"runId": "CLK-x", "advanceMinutes": 0},
    {"runId": "CLK-x", "advanceMinutes": 241},
    {"runId": "CLK-x", "reset": False},
    {"runId": "CLK-x", "reset": True, "offsetSeconds": 5},
])
def test_demo_clock_request_validation(body):
    import api_models

    with pytest.raises(ValidationError):
        api_models.DemoClockRequest(**body)


@pytest.mark.parametrize("body", [
    {"runId": "CLK-x", "anchor": "18:16"},
    {"runId": "CLK-x", "advanceMinutes": 4},
    {"runId": "CLK-x", "reset": True},
])
def test_demo_clock_request_accepts_one_action(body):
    import api_models

    api_models.DemoClockRequest(**body)
