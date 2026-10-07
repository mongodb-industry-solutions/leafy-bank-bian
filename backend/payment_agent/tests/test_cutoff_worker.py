"""Cutoff worker (cutoff plan C3): sweep, stale-case closing, change stream, gate.

The agent run itself is patched (`cutoff_agent.investigate` records the call and marks the
case started); `_snapshot` runs for real over the C2 fake world so trigger keys are the real
status|phase|risk|blocker.
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest
from pymongo.errors import OperationFailure

import cutoff_agent as ca
import cutoff_cases as cases
import cutoff_evidence as ev
import cutoff_worker as cwk
from tests._fakes import FakeColl
from tests.test_cutoff_agent import PID, RUN, TIMING, _approval_request, _world, et, set_clock


@pytest.fixture(autouse=True)
def pinned_timing(monkeypatch):
    monkeypatch.setattr(ev, "stage_timing", lambda db, *, rail, status, at: dict(TIMING[status]))


@pytest.fixture
def runs(monkeypatch):
    calls = []

    def fake_investigate(agent, db, case_id, payment_id):
        calls.append((case_id, payment_id))
        cases.set_agent_fields(db, case_id, {"startedAt": "now"})
        return cases.get(db, case_id).get("agent")  # the real one returns agent{}; None = failed

    monkeypatch.setattr(ca, "investigate", fake_investigate)
    return calls


def _held(at=None):
    at = at or et(17, 5)
    db = _world("PENDING_APPROVAL", "DOMESTIC", at)
    _approval_request(db, at)
    return db


def _case(db):
    return db["cutoffCases"].find_one({"paymentId": PID, "active": True})


# --- sweep ---------------------------------------------------------------------------------

def test_sweep_opens_a_case_and_runs_the_agent_once(runs):
    db = _held()
    assert cwk.sweep_once(object(), db) == {"evaluated": 1, "investigated": 1, "closed": 0}
    case = _case(db)
    assert runs == [(case["caseId"], PID)]
    assert case["trigger"]["key"].startswith("PENDING_APPROVAL|BEFORE_INTERNAL|")
    assert case["agent"]["lease"] is None  # released after the run
    assert cwk.sweep_once(object(), db)["investigated"] == 0  # same key: no rerun
    assert len(runs) == 1


def test_untagged_or_decided_payments_are_not_candidates(runs):
    db = _world("PENDING_APPROVAL", "DOMESTIC", et(17, 5), tagged=False)
    assert cwk.sweep_once(object(), db)["evaluated"] == 0
    db = _held()
    db["payments"].update_one({"paymentId": PID}, {"$set": {"cutoff.decision": "HOLD_NEXT_VALUE_DATE"}})
    assert cwk.sweep_once(object(), db)["evaluated"] == 0
    assert not runs and db["cutoffCases"].count_documents({}) == 0


def test_a_trigger_key_change_reruns_on_the_same_case(runs):
    db = _held()
    cwk.sweep_once(object(), db)
    case_id = _case(db)["caseId"]
    db["payments"].update_one({"paymentId": PID}, {"$set": {"status": "CUTOFF_EXCEPTION"}})
    set_clock(db, et(18, 20))
    assert cwk.sweep_once(object(), db)["investigated"] == 1
    assert [c[0] for c in runs] == [case_id, case_id]
    assert _case(db)["trigger"]["key"].startswith("CUTOFF_EXCEPTION|AFTER_INTERNAL|")


@pytest.mark.parametrize("attempts,age_s,reruns", [(1, 61, True), (1, 30, False), (5, 600, False)])
def test_failed_runs_are_retried_up_to_five_times_after_60s(runs, attempts, age_s, reruns):
    db = _held()
    cwk.sweep_once(object(), db)
    now = datetime.now(timezone.utc)
    cases.set_agent_fields(db, _case(db)["caseId"], {"error": {
        "message": "x", "at": now - timedelta(seconds=age_s), "attempts": attempts}})
    assert cwk.sweep_once(object(), db, real_now=now)["investigated"] == int(reruns)


def test_a_live_lease_held_elsewhere_blocks_the_run(runs):
    db = _held()
    payment = db["payments"].find_one({"paymentId": PID})
    case = cases.open_or_get(db, payment=payment, risk=None, value_date="2026-10-06",
                             business_at=et(17, 5))
    assert cases.claim_lease(db, case["caseId"], "other-instance")
    assert cwk.sweep_once(object(), db)["investigated"] == 0 and not runs


def test_an_in_process_claim_blocks_the_run(runs):
    db = _held()
    payment = db["payments"].find_one({"paymentId": PID})
    case = cases.open_or_get(db, payment=payment, risk=None, value_date="2026-10-06",
                             business_at=et(17, 5))
    assert cwk._claim(case["caseId"])
    try:
        assert cwk.sweep_once(object(), db)["investigated"] == 0
    finally:
        cwk._release(case["caseId"])


def test_a_key_change_seen_while_claimed_reruns_on_the_next_sweep(runs):
    db = _held()
    cwk.sweep_once(object(), db)
    case_id = _case(db)["caseId"]
    db["payments"].update_one({"paymentId": PID}, {"$set": {"status": "CUTOFF_EXCEPTION"}})
    set_clock(db, et(18, 20))
    assert cwk._claim(case_id)
    try:
        assert cwk.sweep_once(object(), db)["investigated"] == 0
    finally:
        cwk._release(case_id)
    assert cwk.sweep_once(object(), db)["investigated"] == 1
    assert _case(db)["trigger"]["ranKey"].startswith("CUTOFF_EXCEPTION|AFTER_INTERNAL|")


def test_a_key_change_while_paused_reruns_after_reject(runs, monkeypatch):
    db = _held()
    cwk.sweep_once(object(), db)
    case_id = _case(db)["caseId"]
    paused = {case_id}
    monkeypatch.setattr(ca, "is_awaiting_approval", lambda agent, cid: cid in paused)
    db["payments"].update_one({"paymentId": PID}, {"$set": {"status": "CUTOFF_EXCEPTION"}})
    set_clock(db, et(18, 20))
    assert cwk.sweep_once(object(), db)["investigated"] == 0  # paused: left alone
    paused.discard(case_id)  # the operator REJECTs
    assert cwk.sweep_once(object(), db)["investigated"] == 1
    assert len(runs) == 2


def test_a_failed_run_does_not_mark_the_key_as_run(runs, monkeypatch):
    db = _held()
    monkeypatch.setattr(ca, "investigate", lambda *a: None)
    assert cwk.sweep_once(object(), db)["investigated"] == 0
    assert "ranKey" not in _case(db)["trigger"]


# --- stale cases ---------------------------------------------------------------------------

@pytest.mark.parametrize("fields,expected", [
    ({"status": "REJECTED", "lifecycle.events": [{"reason": cwk.SUPERSEDED_REASON}]}, cases.SUPERSEDED),
    ({"status": "REJECTED", "lifecycle.events": [{"reason": "Declined."}]}, cases.REJECTED),
    ({"cutoff.decision": "HOLD_NEXT_VALUE_DATE"}, cases.HELD_NEXT_VALUE_DATE),
    ({"status": "ROUTED", "cutoff.decision": "NEXT_VALUE_DATE"}, cases.DEFERRED_NEXT_BUSINESS_DAY),
    ({"status": "SUBMITTED"}, cases.SUBMITTED_IN_TIME),
    ({"status": "ROUTED"}, cases.RELEASED),
    ({}, None),
])
def test_stale_outcome_mapping(fields, expected):
    db = _held()
    if fields:
        db["payments"].update_one({"paymentId": PID}, {"$set": fields})
    assert cwk.stale_outcome(db, {"paymentId": PID, "clockRunId": RUN}) == expected


_STATUSES = ["PENDING_FUNDS", "ROUTED", "AUTHORISED", "SUBMITTED", "SETTLED", "RECONCILED", "REJECTED"]
_BLOCKED = {"PENDING_FUNDS": None, "ROUTED": cases.RELEASED, "AUTHORISED": cases.RELEASED}
_SENT = {"SUBMITTED", "SETTLED", "RECONCILED"}


def _expected_outcome(decision, status):
    if decision == "HOLD_NEXT_VALUE_DATE":
        return cases.HELD_NEXT_VALUE_DATE
    if decision == "NEXT_VALUE_DATE":
        return cases.DEFERRED_NEXT_BUSINESS_DAY
    if status == "REJECTED":
        return cases.REJECTED
    return cases.SUBMITTED_IN_TIME if status in _SENT else _BLOCKED[status]


@pytest.mark.parametrize("status", _STATUSES)
@pytest.mark.parametrize("decision", [None, "EXPEDITE", "HOLD_NEXT_VALUE_DATE", "NEXT_VALUE_DATE"])
def test_stale_outcome_decision_by_status_matrix(decision, status):
    db = _held()
    fields = {"status": status}
    if status == "REJECTED":
        fields["lifecycle.events"] = [{"reason": "Release failed validation."}]
    if decision:
        fields["cutoff.decision"] = decision
    db["payments"].update_one({"paymentId": PID}, {"$set": fields})
    assert cwk.stale_outcome(db, {"paymentId": PID, "clockRunId": RUN}) == _expected_outcome(decision, status)


@pytest.mark.parametrize("decision", [None, "EXPEDITE", "HOLD_NEXT_VALUE_DATE", "NEXT_VALUE_DATE"])
def test_superseded_beats_every_decision(decision):
    db = _held()
    fields = {"status": "REJECTED", "lifecycle.events": [{"reason": cwk.SUPERSEDED_REASON}]}
    if decision:
        fields["cutoff.decision"] = decision
    db["payments"].update_one({"paymentId": PID}, {"$set": fields})
    assert cwk.stale_outcome(db, {"paymentId": PID, "clockRunId": RUN}) == cases.SUPERSEDED


@pytest.mark.parametrize("kind,decision,expected", [
    ("NONE", None, cases.NO_ACTION_NEEDED),
    ("NONE", "EXPEDITE", cases.SUBMITTED_IN_TIME),
    ("NEEDS_APPROVAL", None, cases.SUBMITTED_IN_TIME),
    (None, None, cases.SUBMITTED_IN_TIME),
])
def test_submitted_with_no_action_recommendation(kind, decision, expected):
    db = _held()
    fields = {"status": "SUBMITTED"}
    if decision:
        fields["cutoff.decision"] = decision
    db["payments"].update_one({"paymentId": PID}, {"$set": fields})
    case = {"paymentId": PID, "clockRunId": RUN, "agent": {"recommendation": {"kind": kind}}}
    assert cwk.stale_outcome(db, case) == expected


def test_a_released_payment_is_not_a_candidate(runs):
    db = _held()
    db["payments"].update_one({"paymentId": PID}, {"$set": {"demo.release": {"state": "CLAIMED"}}})
    assert cwk.candidate_query()["demo.release"] == {"$exists": False}
    assert cwk.sweep_once(object(), db)["evaluated"] == 0
    assert not runs and db["cutoffCases"].count_documents({}) == 0


def test_a_next_day_open_is_outside_every_scenario_time_of_day_window():
    # Scenario anchors (transactions `cutoff_scenarios`, ET minutes: 16:30 to 18:35).
    open_tod = 9 * 60
    for anchor in (16 * 60 + 30, 16 * 60 + 45, 17 * 60 + 5, 17 * 60 + 50, 18 * 60 + 35):
        lo, hi = ev._tod_bounds(et(anchor // 60, anchor % 60))
        assert not lo <= ev._tod(et(9, 0)) <= hi
        assert not lo <= open_tod <= hi


def test_a_gone_clock_run_expires_the_case():
    db = _held()
    db["demoClocks"] = FakeColl()
    assert cwk.stale_outcome(db, {"paymentId": PID, "clockRunId": RUN}) == cases.CLOCK_EXPIRED


def test_stale_cases_close_through_supersede(runs, monkeypatch):
    db = _held()
    cwk.sweep_once(object(), db)
    case_id = _case(db)["caseId"]
    paused = {case_id}
    resumed = []
    monkeypatch.setattr(ca, "is_awaiting_approval", lambda agent, cid: cid in paused)
    monkeypatch.setattr(ca, "resume", lambda agent, db, cid, decision, note=None, by="operator":
                        resumed.append((cid, decision)) or paused.discard(cid))
    db["payments"].update_one({"paymentId": PID}, {"$set": {
        "status": "REJECTED", "lifecycle.events": [{"reason": cwk.SUPERSEDED_REASON}]}})
    assert cwk.sweep_once(object(), db)["closed"] == 1
    closed = cases.get(db, case_id)
    assert resumed == [(case_id, ca.SUPERSEDED)]
    assert closed["active"] is False and closed["outcome"]["result"] == cases.SUPERSEDED
    assert closed["agent"]["lease"] is None


def test_superseded_reason_mirrors_transactions():
    src = (pathlib.Path(__file__).resolve().parents[2] / "transactions" / "contexts"
           / "payment_orchestration" / "application" / "cutoff_scenarios.py").read_text()
    assert re.search(r'^SUPERSEDED_REASON = "(.*)"$', src, re.M).group(1) == cwk.SUPERSEDED_REASON


# --- change stream -------------------------------------------------------------------------

class _Stream:
    def __init__(self, items, token="idle-token", fail=None):
        self.items, self.resume_token, self.fail = list(items), token, fail

    @property
    def alive(self):
        return bool(self.items) or self.fail is not None

    def try_next(self):
        if self.items:
            return self.items.pop(0)
        if self.fail is not None:
            exc, self.fail = self.fail, None
            raise exc
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Payments(FakeColl):
    def __init__(self, base, stream):
        super().__init__()
        self.docs, self.stream, self.watched = base.docs, stream, []

    def watch(self, pipeline, **kw):
        self.watched.append((pipeline, kw))
        return self.stream


def _stream_db(stream):
    db = _held()
    db["payments"] = _Payments(db["payments"], stream)
    db["cutoffAgentState"] = FakeColl([{"_id": cases.STREAM_STATE_ID, "resumeToken": "saved"}])
    return db


def _token(db):
    return cwk.load_token(db)


def test_stream_runs_tagged_events_and_persists_each_token(runs, monkeypatch):
    swept = []
    monkeypatch.setattr(cwk, "sweep_once", lambda agent, db, **kw: swept.append(kw))
    events = [{"_id": "t1", "fullDocument": {"paymentId": "PAY-x", "demo": None}},
              {"_id": "t2", "fullDocument": {"paymentId": PID, "demo": {"clockRunId": RUN}}}]
    db = _stream_db(_Stream(events))
    cwk.run_cutoff_stream(object(), db, once=True)
    pipeline, kw = db["payments"].watched[0]
    assert pipeline[0]["$match"]["updateDescription.updatedFields.status"] == {"$exists": True}
    assert kw["full_document"] == "updateLookup" and kw["resume_after"] == "saved"
    assert swept == [{"payment_id": PID, "source": cases.SOURCE_STREAM}]
    assert _token(db) == "t2"


def test_operation_failure_clears_the_token(monkeypatch, caplog):
    db = _stream_db(_Stream([], fail=OperationFailure("token gone")))
    cwk.run_cutoff_stream(object(), db, once=True)
    assert _token(db) is None
    assert any(r.levelname == "CRITICAL" for r in caplog.records)


def test_idle_checkpoint_saves_the_resume_token_every_interval():
    class _Idle:
        resume_token = "post-batch"

        def __init__(self):
            self.polls = 0

        @property
        def alive(self):
            return self.polls < 4

        def try_next(self):
            self.polls += 1
            return None

    saved, ticks = [], iter([0, 10, 31, 31, 40, 70, 70])
    list(cwk.iter_with_idle_checkpoint(_Idle(), saved.append, interval=30, clock=lambda: next(ticks)))
    assert saved == ["post-batch", "post-batch"]


def test_worker_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_CUTOFF_AGENT", raising=False)
    assert cwk.start_cutoff_worker(object(), object()) == []
    monkeypatch.setenv("ENABLE_CUTOFF_AGENT", "true")
    assert cwk.start_cutoff_worker(None, object()) == []
