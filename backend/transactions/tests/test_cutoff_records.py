"""Cutoff plan A3: the hold records (`approvalRequests`, `screeningQueue`) and stage events.

The hold state on `payments` is the truth; these are best-effort views the cutoff agent reads.
Driven through the real `initiate_payment` with a `clock_run_id`, as in `test_cutoff_holds`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from contexts.fraud_evaluation.domain import fraud_rules
from contexts.payment_order_initiation.domain import duplicate_detection
from data import ensure_indexes
from services.payments_service import PaymentsService
from shared import business_clock, hold_queues, stage_events
from tests.test_cutoff_holds import (
    APPROVER,
    POTENTIAL,
    _db,
    _domestic,
    _move_run,
    _run_at,
    _stored,
)
from tests.test_payments_service import (
    DEBTOR,
    FakeCollection,
    FakeConnection,
    _initiate,
)

CA_BANK = {"bic": "ROYCCAT2", "bankName": "Royal Bank of Canada", "bankCountry": "CA"}

STAFF = [
    {"staffId": "STAFF-maya", "role": "APPROVER", "primaryFor": [DEBTOR],
     "customerId": APPROVER},
    {"staffId": "STAFF-raj", "role": "APPROVER", "shift": {"startEt": "09:00", "endEt": "19:00"}},
    {"staffId": "STAFF-analyst-1", "role": "ANALYST",
     "shift": {"startEt": "08:00", "endEt": "17:00"}},
    {"staffId": "STAFF-analyst-3", "role": "ANALYST",
     "shift": {"startEt": "12:00", "endEt": "21:00"}},
]


@pytest.fixture
def db():
    db = _db()
    db["staffDirectory"] = FakeCollection(STAFF, key="staffId")
    return db


@pytest.fixture
def service(db):
    return PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=5_000_000.0)


def _approval_held(service, db, hh=10, mm=0):
    return _domestic(service, _run_at(db, hh, mm), instructed_amount=25_000.0)


def _screening_held(service, db, hh=10, mm=0):
    return _domestic(service, _run_at(db, hh, mm), creditor_party=POTENTIAL)


def _records(db, name, payment):
    return [d for d in db[name].docs if d["paymentId"] == payment["paymentId"]]


def _et(hh, mm, day=None):
    day = day or business_clock.to_et(datetime.now(timezone.utc)).date()
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=business_clock.ET)


# --- approval requests --------------------------------------------------------

def test_approval_hold_opens_one_request(service, db):
    held = _approval_held(service, db)

    [request] = _records(db, "approvalRequests", held)
    assert request["status"] == hold_queues.OPEN
    assert request["approvalRequestId"].startswith("APR-")
    assert request["requiredApprovers"] == [APPROVER]
    assert request["primaryApprover"] == request["assignedTo"] == "STAFF-maya"
    assert request["amount"] == 25_000.0
    assert request["demo"]["clockRunId"] == held["demo"]["clockRunId"]
    assert request["requestedAt"] == _stored(db, held)["lifecycle"]["stateEnteredAt"]


def test_approve_closes_request_before_resume(service, db, monkeypatch):
    held = _approval_held(service, db)
    seen = {}
    from process import payment_lifecycle
    real_run = payment_lifecycle.run

    def spy(ctx, **kw):
        seen["status"] = _records(db, "approvalRequests", held)[0]["status"]
        return real_run(ctx, **kw)

    monkeypatch.setattr(payment_lifecycle, "run", spy)
    service.approve_payment(held["paymentId"], approver_id=APPROVER, decision="APPROVED")

    assert seen["status"] == hold_queues.APPROVED
    [request] = _records(db, "approvalRequests", held)
    assert request["resolvedBy"] == APPROVER and request["resolvedAt"] is not None


def test_approve_rejected_closes_request(service, db):
    held = _approval_held(service, db)

    service.approve_payment(held["paymentId"], approver_id=APPROVER, decision="REJECTED")

    [request] = _records(db, "approvalRequests", held)
    assert (request["status"], request["resolvedBy"]) == (hold_queues.REJECTED, APPROVER)


# --- screening queue ----------------------------------------------------------

def test_screening_hold_enqueues_with_on_shift_analyst(service, db):
    # International (internal cut-off 18:15), so 17:50 reaches screening — C2's shape.
    held = _domestic(service, _run_at(db, 17, 50), creditor_party=dict(POTENTIAL, **CA_BANK))
    assert held["status"] == "PENDING_SCREENING"

    [item] = _records(db, "screeningQueue", held)
    assert item["status"] == hold_queues.OPEN
    assert item["screeningItemId"].startswith("SQ-")
    assert item["matched"] == "NORTHGATE TRADING FZE"
    assert item["reason"] == _stored(db, held)["screening"]["reason"]
    assert item["assignedAnalyst"] == "STAFF-analyst-3", "analyst 1 is off shift at 17:50"
    assert item["synthetic"] is False
    assert item["demo"]["clockRunId"] == held["demo"]["clockRunId"]


def test_screening_hit_closes_item(service, db):
    held = _screening_held(service, db)

    service.resolve_screening(held["paymentId"], analyst_id="ANALYST-1", outcome="HIT")

    [item] = _records(db, "screeningQueue", held)
    assert (item["status"], item["resolvedBy"]) == (hold_queues.HIT, "ANALYST-1")


@pytest.mark.parametrize("branch", ["resume", "cutoff_exception", "next_value_date"])
def test_screening_clear_closes_item_on_every_branch(service, db, branch):
    run_id = _run_at(db, 17 if branch == "cutoff_exception" else 10, 0)
    held = _domestic(service, run_id, creditor_party=POTENTIAL)
    if branch == "cutoff_exception":
        _move_run(db, run_id, 17, 40)
    if branch == "next_value_date":
        service.hold_next_value_date(held["paymentId"], decided_by="OPS-1", reason="r")

    resolved = service.resolve_screening(held["paymentId"], analyst_id="ANALYST-1",
                                         outcome="CLEAR")

    expected = {"resume": "IN_PROGRESS", "cutoff_exception": "CUTOFF_EXCEPTION",
                "next_value_date": "ROUTED"}[branch]
    assert resolved["status"] == expected
    [item] = _records(db, "screeningQueue", held)
    assert item["status"] == hold_queues.CLEARED


def test_record_write_failure_never_breaks_the_hold(service, db):
    class Broken(FakeCollection):
        def insert_one(self, *a, **kw):
            raise RuntimeError("boom")

    db["approvalRequests"] = Broken()
    db["screeningQueue"] = Broken()
    db["paymentStageEvents"] = Broken()

    assert _approval_held(service, db)["status"] == "PENDING_APPROVAL"
    assert _screening_held(service, db)["status"] == "PENDING_SCREENING"


def test_untagged_writes_no_records_or_events(service, db):
    _domestic(service, instructed_amount=25_000.0)
    _domestic(service, creditor_party=POTENTIAL)

    assert db["approvalRequests"].docs == []
    assert db["screeningQueue"].docs == []
    assert db["paymentStageEvents"].docs == []


# --- stage events -------------------------------------------------------------

def test_tagged_transitions_append_events_with_prev_stage_and_duration(service, db):
    held = _approval_held(service, db)
    service.approve_payment(held["paymentId"], approver_id=APPROVER, decision="APPROVED")

    payment = _stored(db, held)
    events = [e for e in db["paymentStageEvents"].docs if e["paymentId"] == held["paymentId"]]
    lifecycle_events = payment["lifecycle"]["events"]
    assert [e["toState"] for e in events] == [e["state"] for e in lifecycle_events[1:]]
    assert [e["meta"]["stage"] for e in events] == [e["state"] for e in lifecycle_events[:-1]]
    for event in events:
        assert event["source"] == "LIVE"
        assert event["clockRunId"] == held["demo"]["clockRunId"]
        assert event["durationMs"] >= 0
        assert event["meta"]["rail"] == "WIRE"
    approved = next(e for e in events if e["meta"]["stage"] == "PENDING_APPROVAL")
    assert approved["meta"]["segment"] == "COMMERCIAL"


def test_stage_event_duration_and_naive_datetimes():
    coll = FakeCollection()
    entered = datetime(2026, 10, 6, 21, 0)               # naive: read as UTC
    payment = {"paymentId": "P", "rail": "WIRE", "lifecycle": {"events": [
        {"state": "ROUTED", "at": entered},
        {"state": "PENDING_SCREENING", "at": entered + timedelta(minutes=4)},
    ]}}

    stage_events.append(coll, payment, at=entered + timedelta(minutes=4))

    [event] = coll.docs
    assert event["durationMs"] == 4 * 60 * 1000
    assert event["at"].tzinfo is not None
    assert (event["meta"]["stage"], event["toState"]) == ("ROUTED", "PENDING_SCREENING")


def test_session_bound_advance_writes_no_event(service, db):
    payment = _initiate(service, clock_run_id=_run_at(db, 10, 0))

    assert payment["status"] == "SETTLED"
    events = [e for e in db["paymentStageEvents"].docs if e["paymentId"] == payment["paymentId"]]
    assert events, "the tagged transitions outside the session still record"
    assert "SETTLED" not in {e["toState"] for e in events}


# --- queue position -----------------------------------------------------------

def _item(db, pid, *, run, minutes_ago, priority=0, at):
    hold_queues.enqueue_screening(
        db, payment={"paymentId": pid, "demo": {"clockRunId": run}}, reason="r", matched=None,
        at=at, synthetic=True, queued_at=at - timedelta(minutes=minutes_ago), priority=priority,
    )


def test_position_by_priority_then_age_within_run(db):
    at = _et(17, 50)
    _item(db, "A", run="R1", minutes_ago=30, at=at)
    _item(db, "B", run="R1", minutes_ago=10, at=at)
    _item(db, "C", run="R1", minutes_ago=5, priority=1, at=at)
    _item(db, "D", run="R1", minutes_ago=1, at=at)

    position = hold_queues.screening_position(db, payment_id="D", at=at)

    assert position == {"position": 4, "ahead": 3, "analystsOnShift": 1}
    assert hold_queues.screening_position(db, payment_id="C", at=at)["position"] == 1
    assert hold_queues.screening_position(db, payment_id="B", at=at)["position"] == 3


def test_position_ignores_other_runs(db):
    at = _et(10, 0)
    _item(db, "OTHER", run="R2", minutes_ago=30, priority=1, at=at)
    _item(db, "MINE", run="R1", minutes_ago=1, at=at)

    assert hold_queues.screening_position(db, payment_id="MINE", at=at)["position"] == 1


def test_on_shift_respects_window_and_out_of_office():
    raj = STAFF[1]
    assert hold_queues.on_shift(raj, _et(18, 16))
    assert not hold_queues.on_shift(raj, _et(19, 0))
    away = dict(raj, outOfOfficeUntil=_et(18, 30))
    assert not hold_queues.on_shift(away, _et(18, 16))
    assert not hold_queues.on_shift(STAFF[0], _et(12, 0)), "no shift window, never on shift"


# --- run-scoped duplicate and velocity ----------------------------------------

def _filter_args(run_id):
    now = datetime.now(timezone.utc)
    return dict(
        dup=duplicate_detection.recent_duplicate_filter(
            debtor_account_id=DEBTOR, creditor_account_no=None, instructed_amount=250.0,
            instructed_currency="USD", now=now, exclude_payment_id="X", clock_run_id=run_id),
        vel=fraud_rules.velocity_filter(debtor_account_id=DEBTOR, now=now,
                                        exclude_payment_id="X", clock_run_id=run_id),
    )


def test_tagged_duplicate_and_velocity_scoped_to_run():
    filters = _filter_args("CLK-1")
    assert filters["dup"]["demo.clockRunId"] == "CLK-1"
    assert filters["vel"]["demo.clockRunId"] == "CLK-1"


def test_rerun_in_a_new_run_is_not_a_duplicate_of_the_last(service, db):
    first = _domestic(service, _run_at(db, 10, 0))
    second = _domestic(service, _run_at(db, 10, 0))

    assert first["status"] == second["status"] == "IN_PROGRESS"
    assert second["idempotency"]["duplicateOf"] is None


def test_untagged_duplicate_ignores_tagged(service, db):
    _domestic(service, _run_at(db, 10, 0))
    untagged = _domestic(service)
    again = _domestic(service)

    assert untagged["idempotency"]["duplicateOf"] is None
    assert again["idempotency"]["duplicateOf"] == untagged["paymentId"]
    assert _filter_args(None)["vel"]["demo.clockRunId"] is None


# --- ensure_indexes -----------------------------------------------------------

@pytest.mark.parametrize("specs", [
    ensure_indexes.APPROVAL_REQUESTS_INDEXES, ensure_indexes.SCREENING_QUEUE_INDEXES,
])
def test_hold_queues_declare_unique_partial_open_index(specs):
    unique = next(s for s in specs if s.get("unique"))
    assert unique["keys"] == [("paymentId", 1)]
    assert unique["partialFilterExpression"] == {"status": {"$eq": "OPEN"}}


def test_registry_covers_the_a3_collections(monkeypatch):
    seen = []
    monkeypatch.setattr(ensure_indexes, "_ensure",
                        lambda conn, db_name, coll, specs: seen.append(coll) or [])
    monkeypatch.setattr(ensure_indexes, "_ensure_time_series", lambda *a, **kw: None)

    ensure_indexes.ensure_transactions_indexes(FakeConnection(None), "db")

    assert {"approvalRequests", "screeningQueue", "staffDirectory",
            "paymentStageEvents"} <= set(seen)


class _ListingDb:
    def __init__(self, existing):
        self.existing, self.created = existing, []

    def list_collections(self, filter):
        return [c for c in self.existing if c["name"] == filter["name"]]

    def create_collection(self, name, **kw):
        self.created.append((name, kw))


def test_time_series_created_when_absent():
    db = _ListingDb([])
    ensure_indexes._ensure_time_series(db, "paymentStageEvents",
                                       timeseries=ensure_indexes.STAGE_EVENTS_TIMESERIES,
                                       expire_after_seconds=30 * 86400)
    [(name, kw)] = db.created
    assert kw["timeseries"] == {"timeField": "at", "metaField": "meta", "granularity": "minutes"}
    assert kw["expireAfterSeconds"] == 30 * 86400


def test_time_series_refuses_a_regular_collection():
    db = _ListingDb([{"name": "paymentStageEvents", "type": "collection"}])
    with pytest.raises(RuntimeError, match="regular collection"):
        ensure_indexes._ensure_time_series(db, "paymentStageEvents",
                                           timeseries=ensure_indexes.STAGE_EVENTS_TIMESERIES,
                                           expire_after_seconds=1)
    assert db.created == []
    # An existing time series is left alone.
    ok = _ListingDb([{"name": "paymentStageEvents", "type": "timeseries"}])
    ensure_indexes._ensure_time_series(ok, "paymentStageEvents", timeseries={},
                                       expire_after_seconds=1)
    assert ok.created == []

