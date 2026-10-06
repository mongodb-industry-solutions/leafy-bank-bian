"""Cutoff Agent deterministic core (cutoff plan C1): window, clock, risk, rules, evidence.

Mirrored logic is pinned against the transactions originals, loaded by file path.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from pymongo.errors import DuplicateKeyError

import cutoff_clock as clock
import cutoff_evidence as ev
import cutoff_risk as cr
import cutoff_rules as rules
import cutoff_window as cw
from tests import _transactions
from tests._fakes import FakeColl, FakeDB

TX_POLICY = _transactions.load("contexts/payment_orchestration/domain/cutoff_policy.py",
                               "cutoff_policy")
TX_QUEUES = _transactions.load("shared/hold_queues.py", "hold_queues")

DAY = date(2026, 10, 6)  # a Tuesday, EDT


def et(h, m, day=DAY):
    return datetime.combine(day, time(h, m), tzinfo=cw.ET).astimezone(timezone.utc)


DOMESTIC = cw.window_for(rail="WIRE", wire_type="DOMESTIC", currency="USD")
INTL = cw.window_for(rail="WIRE", wire_type="INTERNATIONAL", currency="USD")


# --- window parity ------------------------------------------------------------------------

def test_policy_table_matches_transactions():
    assert cw._POLICY == TX_POLICY._POLICY
    assert cw._EXTERNAL_NETWORK == TX_POLICY._EXTERNAL_NETWORK


@pytest.mark.parametrize("rail,wire_type,currency", [
    ("WIRE", "DOMESTIC", "USD"), ("wire", "domestic", None), ("WIRE", "INTERNATIONAL", "USD"),
    ("WIRE", "INTERNATIONAL", "EUR"), ("WIRE", None, "USD"), ("ACH", "DOMESTIC", "USD"),
    (None, None, None), ("WIRE", "CHIPS", "USD"),
])
def test_window_for_matches_transactions(rail, wire_type, currency):
    mine = cw.window_for(rail=rail, wire_type=wire_type, currency=currency)
    theirs = TX_POLICY.window_for(rail=rail, wire_type=wire_type, currency=currency)
    assert (mine is None) == (theirs is None)
    if mine is not None:
        assert (mine.wire_type, mine.internal_minutes_et, mine.external_minutes_et,
                mine.external_network) == (theirs.wire_type, theirs.internal_minutes_et,
                                           theirs.external_minutes_et, theirs.external_network)


def test_phase_cutoff_at_and_next_business_day_match_transactions():
    tx_window = TX_POLICY.window_for(rail="WIRE", wire_type="DOMESTIC", currency="USD")
    for minute in (17 * 60 + 29, 17 * 60 + 30, 17 * 60 + 31, 18 * 60 + 45, 18 * 60 + 46):
        at = et(minute // 60, minute % 60)
        assert cw.phase(DOMESTIC, at=at) == TX_POLICY.phase(tx_window, at=at)
    for day in (date(2026, 3, 9), date(2026, 11, 2), DAY):  # both DST edges
        for which in ("internal", "external"):
            assert cw.cutoff_at(DOMESTIC, business_date=day, which=which) == \
                TX_POLICY.cutoff_at(tx_window, business_date=day, which=which)
    for offset in range(7):
        d = DAY + timedelta(days=offset)
        assert cw.next_business_day(d) == TX_POLICY.next_business_day(d)


def test_window_for_payment_reads_wire_details():
    payment = {"rail": "WIRE", "wireDetails": {"wireType": "INTERNATIONAL"},
               "instructedCurrency": "USD"}
    assert cw.window_for_payment(payment).wire_type == "INTERNATIONAL_USD"
    assert cw.window_for_payment({"rail": "WIRE", "instructedCurrency": "USD"}) is None


# --- clock --------------------------------------------------------------------------------

def test_clock_now_applies_offset_uncached_and_none_when_run_gone():
    db = FakeDB()
    db["demoClocks"] = FakeColl([{"_id": "CLK-1", "offsetSeconds": 600,
                                  "businessDate": "2026-10-02"}])
    real = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    assert clock.now(db, "CLK-1", real_now=real) == real + timedelta(minutes=10)
    db["demoClocks"].update_one({"_id": "CLK-1"}, {"$set": {"offsetSeconds": 1200}})
    assert clock.now(db, "CLK-1", real_now=real) == real + timedelta(minutes=20)
    assert clock.now(db, "CLK-gone", real_now=real) is None
    assert clock.now(db, None, real_now=real) is None


def test_business_date_prefers_the_run_record():
    at = et(17, 5)
    assert clock.business_date({"businessDate": "2026-10-02"}, at) == date(2026, 10, 2)
    assert clock.business_date({}, at) == DAY
    assert clock.business_date(None, et(23, 30)) == DAY  # ET, not UTC


# --- risk ---------------------------------------------------------------------------------

def _assess(**kw):
    base = dict(status=cr.PENDING_APPROVAL, window=DOMESTIC, at=et(16, 0), minutes_in_stage=0,
                remaining_p50_min=5, remaining_p90_min=10)
    return cr.assess(**{**base, **kw})


def test_no_window_means_no_risk():
    assert _assess(window=None) is None


def test_on_track_before_internal():
    r = _assess()
    assert (r["phase"], r["riskLevel"], r["blockerType"]) == (
        cw.BEFORE_INTERNAL, cr.ON_TRACK, "APPROVAL")
    assert r["minutesToInternal"] == 90 and r["minutesToExternal"] == 165


def test_at_risk_when_p90_overruns_internal():
    r = _assess(at=et(17, 25))
    assert r["riskLevel"] == cr.AT_RISK


def test_will_miss_when_p50_overruns_external_for_a_pending_hold():
    r = _assess(at=et(17, 0), stage_p50_min=120, stage_p90_min=200)
    assert r["riskLevel"] == cr.WILL_MISS


def test_after_internal_is_at_least_at_risk():
    r = _assess(status=cr.CUTOFF_EXCEPTION, at=et(18, 0))
    assert (r["phase"], r["riskLevel"]) == (cw.AFTER_INTERNAL, cr.AT_RISK)


def test_cutoff_exception_uses_p90_against_external():
    r = _assess(status=cr.CUTOFF_EXCEPTION, at=et(18, 40), remaining_p50_min=3,
                remaining_p90_min=6)
    assert r["riskLevel"] == cr.WILL_MISS
    pending = _assess(status=cr.PENDING_SCREENING, at=et(18, 40), remaining_p50_min=3,
                      remaining_p90_min=6, window=INTL)
    assert pending["riskLevel"] == cr.AT_RISK  # pending holds judge external on p50


def test_after_external_will_miss():
    r = _assess(at=et(18, 46), remaining_p50_min=0, remaining_p90_min=0)
    assert (r["phase"], r["riskLevel"]) == (cw.AFTER_EXTERNAL, cr.WILL_MISS)


def test_funds_will_miss_unless_credit_covers_by_internal():
    late = {"shortfall": 3200, "expectedCredits": [
        {"amount": 4000, "status": "EXPECTED", "expectedAt": et(17, 45)}]}
    early = {"shortfall": 3200, "expectedCredits": [
        {"amount": 4000, "status": "EXPECTED", "expectedAt": et(17, 0)}]}
    small = {"shortfall": 3200, "expectedCredits": [
        {"amount": 1000, "status": "EXPECTED", "expectedAt": et(17, 0)}]}
    for funds, level in ((late, cr.WILL_MISS), (early, cr.ON_TRACK), (small, cr.WILL_MISS)):
        assert _assess(status=cr.PENDING_FUNDS, funds=funds)["riskLevel"] == level


def test_screening_residual_and_queue_term():
    r = _assess(status=cr.PENDING_SCREENING, window=INTL, minutes_in_stage=4,
                stage_p50_min=6, stage_p90_min=14, remaining_p50_min=4, remaining_p90_min=11,
                screening={"ahead": 2, "analystsOnShift": 0})
    # residual max(6-4, 1.5)=2 + queue 2*6/max(1,0)=12 + downstream 4
    assert r["remainingP50Min"] == 18.0 and r["queueMinutes"] == 12.0
    assert r["remainingP90Min"] == 10 + 12 + 11
    assert cr.stage_residual(8, 30) == 2.0  # floor: 25% of the stage


# --- rules --------------------------------------------------------------------------------

AT_RISK = {"riskLevel": cr.AT_RISK, "phase": cw.BEFORE_INTERNAL}
WILL_MISS = {"riskLevel": cr.WILL_MISS, "phase": cw.BEFORE_INTERNAL}
AFTER_OK = {"riskLevel": cr.AT_RISK, "phase": cw.AFTER_INTERNAL}
AFTER_MISS = {"riskLevel": cr.WILL_MISS, "phase": cw.AFTER_INTERNAL}


def test_auto_and_gated_are_disjoint_and_routes_cover_gated():
    assert not rules.AUTO & rules.GATED
    assert set(rules.ROUTE_FOR) == set(rules.GATED)


def test_on_track_allows_only_the_assessment():
    on_track = {"riskLevel": cr.ON_TRACK, "phase": cw.BEFORE_INTERNAL}
    assert rules.allowed_auto(risk=on_track, status=cr.PENDING_APPROVAL,
                              backup_on_shift=True) == {rules.RECORD_ASSESSMENT}
    assert rules.allowed_auto(risk=None, status=cr.PENDING_FUNDS) == {rules.RECORD_ASSESSMENT}


def test_approval_caps_and_backup_on_shift():
    got = rules.allowed_auto(risk=AT_RISK, status=cr.PENDING_APPROVAL, backup_on_shift=True)
    assert {rules.SEND_APPROVAL_REMINDER, rules.ESCALATE_TO_BACKUP_APPROVER} <= got
    off = rules.allowed_auto(risk=AT_RISK, status=cr.PENDING_APPROVAL, backup_on_shift=False)
    assert rules.ESCALATE_TO_BACKUP_APPROVER not in off
    spent = rules.allowed_auto(risk=AT_RISK, status=cr.PENDING_APPROVAL, backup_on_shift=True,
                               taken={rules.SEND_APPROVAL_REMINDER: 2,
                                      rules.ESCALATE_TO_BACKUP_APPROVER: 1})
    assert spent == {rules.RECORD_ASSESSMENT}


def test_screening_and_funds_remedies():
    assert rules.RAISE_SCREENING_PRIORITY in rules.allowed_auto(
        risk=WILL_MISS, status=cr.PENDING_SCREENING, screening_ahead=7)
    assert rules.RAISE_SCREENING_PRIORITY not in rules.allowed_auto(
        risk=WILL_MISS, status=cr.PENDING_SCREENING, screening_ahead=0)
    assert rules.allowed_auto(risk=WILL_MISS, status=cr.PENDING_FUNDS,
                              taken={rules.NOTIFY_CUSTOMER_FUNDS: 1}) == {rules.RECORD_ASSESSMENT}


def test_a_remedy_with_no_target_is_not_untried():
    assert rules.untried_remedies(risk=WILL_MISS, status=cr.PENDING_APPROVAL,
                                  backup_on_shift=True, approval_open=False) == frozenset()
    assert rules.untried_remedies(risk=WILL_MISS, status=cr.PENDING_SCREENING,
                                  screening_ahead=None, screening_queued=False) == frozenset()


def test_hold_needs_will_miss_pending_and_no_untried_remedy():
    untried = rules.untried_remedies(risk=WILL_MISS, status=cr.PENDING_FUNDS)
    assert untried == {rules.NOTIFY_CUSTOMER_FUNDS}
    assert "autonomous remedies" in rules.check_proposal(
        rules.HOLD_NEXT_VALUE_DATE, status=cr.PENDING_FUNDS, risk=WILL_MISS, untried=untried)
    assert rules.check_proposal(rules.HOLD_NEXT_VALUE_DATE, status=cr.PENDING_FUNDS,
                                risk=WILL_MISS) is None
    assert rules.check_proposal(rules.HOLD_NEXT_VALUE_DATE, status=cr.PENDING_APPROVAL,
                                risk=AT_RISK)
    assert rules.check_proposal(rules.HOLD_NEXT_VALUE_DATE, status=cr.CUTOFF_EXCEPTION,
                                risk=AFTER_MISS)


def test_expedite_rules():
    ok = dict(status=cr.CUTOFF_EXCEPTION, risk=AFTER_OK)
    assert rules.check_proposal(rules.EXPEDITE, **ok) is None
    assert rules.check_proposal(rules.EXPEDITE, **ok, open_exceptions=1)
    assert rules.check_proposal(rules.EXPEDITE, status=cr.CUTOFF_EXCEPTION, risk=AFTER_MISS)
    assert rules.check_proposal(rules.EXPEDITE, status=cr.PENDING_SCREENING, risk=AFTER_OK)
    assert rules.check_proposal(rules.EXPEDITE, status=cr.CUTOFF_EXCEPTION,
                                risk={"riskLevel": cr.WILL_MISS, "phase": cw.AFTER_EXTERNAL})


def test_defer_rules():
    assert rules.check_proposal(rules.DEFER_NEXT_BUSINESS_DAY, status=cr.CUTOFF_EXCEPTION,
                                risk=AFTER_MISS) is None
    assert rules.check_proposal(rules.DEFER_NEXT_BUSINESS_DAY, status=cr.CUTOFF_EXCEPTION,
                                risk=AFTER_OK, open_exceptions=1) is None
    assert rules.check_proposal(rules.DEFER_NEXT_BUSINESS_DAY, status=cr.CUTOFF_EXCEPTION,
                                risk=AFTER_OK)
    assert rules.check_proposal(rules.DEFER_NEXT_BUSINESS_DAY, status=cr.PENDING_FUNDS,
                                risk=WILL_MISS)


def test_rejected_action_cannot_be_reproposed_in_the_same_phase():
    rejected = [{"action": rules.EXPEDITE, "phase": cw.AFTER_INTERNAL}]
    assert "rejected" in rules.check_proposal(rules.EXPEDITE, status=cr.CUTOFF_EXCEPTION,
                                              risk=AFTER_OK, rejected=rejected)
    other_phase = [{"action": rules.EXPEDITE, "phase": cw.BEFORE_INTERNAL}]
    assert rules.check_proposal(rules.EXPEDITE, status=cr.CUTOFF_EXCEPTION, risk=AFTER_OK,
                                rejected=other_phase) is None


def test_non_gated_or_windowless_proposals_are_refused():
    assert rules.check_proposal(rules.SEND_APPROVAL_REMINDER, status=cr.PENDING_APPROVAL,
                                risk=AT_RISK)
    assert rules.check_proposal(rules.EXPEDITE, status=cr.CUTOFF_EXCEPTION, risk=None)


# --- evidence -----------------------------------------------------------------------------

STAFF = [
    {"staffId": "S-maya", "role": "APPROVER", "outOfOfficeUntil": et(9, 0, DAY + timedelta(1)),
     "primaryFor": ["ACC-1"]},
    {"staffId": "S-raj", "role": "APPROVER", "backupFor": "S-maya",
     "shift": {"startEt": "09:00", "endEt": "19:00"}},
    {"staffId": "A-1", "role": "ANALYST", "shift": {"startEt": "08:00", "endEt": "17:00"}},
    {"staffId": "A-3", "role": "ANALYST", "shift": {"startEt": "12:00", "endEt": "21:00"}},
]


@pytest.mark.parametrize("at", [et(8, 59), et(9, 0), et(16, 59), et(17, 0), et(18, 59),
                                et(19, 0), et(23, 0)])
def test_on_shift_matches_transactions(at):
    for staff in STAFF + [{"staffId": "X"}]:
        assert ev.on_shift(staff, at) == TX_QUEUES.on_shift(staff, at)


def _queue_db():
    db = FakeDB()
    db["staffDirectory"] = FakeColl(STAFF)
    items = [{"paymentId": f"SYN-{i}", "status": "OPEN", "priority": 0,
              "queuedAt": et(17, 50) - timedelta(minutes=5 * (8 - i)),
              "demo": {"clockRunId": "CLK-1"}} for i in range(1, 8)]
    items.append({"paymentId": "PAY-c2", "status": "OPEN", "priority": 0,
                  "queuedAt": et(17, 50), "demo": {"clockRunId": "CLK-1"},
                  "screeningItemId": "SQ-c2"})
    items.append({"paymentId": "PAY-other", "status": "OPEN", "priority": 5,
                  "queuedAt": et(17, 0), "demo": {"clockRunId": "CLK-2"}})
    items.append({"paymentId": "SYN-done", "status": "CLEARED", "priority": 9,
                  "queuedAt": et(17, 0), "demo": {"clockRunId": "CLK-1"}})
    db["screeningQueue"] = FakeColl(items)
    return db


def test_screening_status_matches_transactions_and_respects_priority():
    db = _queue_db()
    at = et(17, 50)
    mine = ev.screening_status(db, payment_id="PAY-c2", at=at)
    theirs = TX_QUEUES.screening_position(db, payment_id="PAY-c2", at=at)
    assert {k: mine[k] for k in theirs} == theirs == {"position": 8, "ahead": 7,
                                                      "analystsOnShift": 1}
    db["screeningQueue"].update_one({"paymentId": "PAY-c2"}, {"$set": {"priority": 1}})
    raised = ev.screening_status(db, payment_id="PAY-c2", at=at)
    assert raised["position"] == 1 and raised["runMaxPriority"] == 1
    assert raised == {**raised, **TX_QUEUES.screening_position(db, payment_id="PAY-c2", at=at)}
    assert ev.screening_status(db, payment_id="PAY-none", at=at) is None


def test_approval_status_primary_backup_and_workload():
    db = FakeDB()
    db["staffDirectory"] = FakeColl(STAFF)
    db["approvalRequests"] = FakeColl(
        [{"paymentId": "PAY-c1", "status": "OPEN", "approvalRequestId": "APR-1",
          "primaryApprover": "S-maya", "assignedTo": "S-maya", "requestedAt": et(17, 5),
          "reminders": [{"at": et(17, 6)}]}]
        + [{"paymentId": f"SYN-{i}", "status": "OPEN", "assignedTo": "S-raj", "synthetic": True}
           for i in range(3)])
    got = ev.approval_status(db, payment_id="PAY-c1", at=et(17, 15))
    assert got["open"] and got["minutesOpen"] == 10.0 and got["reminders"] == 1
    assert got["primary"]["staffId"] == "S-maya" and not got["primary"]["onShift"]
    assert got["backup"]["staffId"] == "S-raj" and got["backup"]["onShift"]
    assert got["backup"]["openRequests"] == 3 and got["backup"]["syntheticOpen"] == 3
    assert ev.approval_status(db, payment_id="PAY-x", at=et(17, 15)) == {"open": False}


def test_funds_position_and_open_exceptions():
    db = FakeDB()
    db["accounts"] = FakeColl([{"accountId": "ACC-3", "balance": {"available": 6300.0},
                                "expectedCredits": [{"amount": 4000.0, "status": "EXPECTED",
                                                     "expectedAtEt": "17:45"}]}])
    db["exceptions"] = FakeColl([
        {"paymentId": "PAY-1", "status": "OPEN", "category": "FRAUD"},
        {"paymentId": "PAY-1", "status": "RESOLVED", "category": "OTHER"}])
    funds = ev.funds_position(db, {"debtor": {"accountId": "ACC-3"}, "instructedAmount": 9500.0},
                              business_date=DAY)
    assert funds["shortfall"] == 3200.0
    assert funds["expectedCredits"][0]["expectedAt"] == et(17, 45)
    assert ev.open_exceptions(db, "PAY-1") == {"count": 1, "categories": ["FRAUD"]}


def _event(pid, stage, minutes, at, source="SEED", rail="WIRE"):
    return {"paymentId": pid, "meta": {"rail": rail, "stage": stage}, "at": at,
            "durationMs": int(minutes * 60000), "source": source}


def test_stage_timing_python_path_filters_and_sums_journeys():
    at = et(18, 0)
    events = [
        # complete journey within the window: 3 + 5 = 8 minutes
        _event("J1", "AUTHORISED", 3, at - timedelta(days=1)),
        _event("J1", "APPROVED", 5, at - timedelta(days=1)),
        # incomplete journey: ignored for the remaining journey
        _event("J2", "AUTHORISED", 1, at - timedelta(days=2)),
        # outside the time-of-day window, after `at`, too old, other rail
        _event("J3", "AUTHORISED", 50, at - timedelta(days=1, hours=3)),
        _event("J4", "AUTHORISED", 50, at + timedelta(minutes=5)),
        _event("J5", "AUTHORISED", 50, at - timedelta(days=20)),
        _event("J6", "AUTHORISED", 50, at - timedelta(days=1), rail="INTERNAL"),
        _event("PAY-live", "PENDING_SCREENING", 9, at - timedelta(minutes=30), source="LIVE"),
        _event("J7", "PENDING_SCREENING", 5, at - timedelta(days=1)),
    ]
    db = FakeDB()
    db["paymentStageEvents"] = FakeColl(events)
    t = ev.stage_timing(db, rail="WIRE", status=cr.PENDING_SCREENING, at=at)
    assert t["method"] == "python" and t["stages"] == ["AUTHORISED", "APPROVED"]
    assert (t["remainingP50Min"], t["remainingP90Min"], t["journeySamples"]) == (8.0, 8.0, 1)
    assert (t["stageP50Min"], t["stageP90Min"], t["stageSamples"]) == (5.0, 9.0, 2)
    assert t["sourceMix"] == {"SEED": 4, "LIVE": 1}
    assert [p["paymentId"] for p in t["liveRecent"]] == ["PAY-live"]


def test_stage_timing_uses_percentile_aggregation_when_available():
    seen = {}

    class AggColl(FakeColl):
        def aggregate(self, pipeline):
            seen["pipeline"] = pipeline
            return iter([{"journeys": [{"n": 12, "p": [600000, 1200000]}],
                          "stage": [{"n": 4, "p": [300000, 540000]}],
                          "mix": [{"_id": "SEED", "n": 40}, {"_id": "LIVE", "n": 2}],
                          "live": []}])

    db = FakeDB()
    db["paymentStageEvents"] = AggColl()
    t = ev.stage_timing(db, rail="WIRE", status=cr.CUTOFF_EXCEPTION, at=et(18, 16))
    assert t["method"] == "$percentile"
    assert (t["remainingP50Min"], t["remainingP90Min"], t["journeySamples"]) == (10.0, 20.0, 12)
    assert (t["stageP50Min"], t["stageP90Min"]) == (5.0, 9.0)
    assert t["sourceMix"] == {"SEED": 40, "LIVE": 2}
    facet = seen["pipeline"][-1]["$facet"]
    assert facet["journeys"][-1]["$group"]["p"]["$percentile"]["p"] == [0.5, 0.9]


def test_percentile_helper_nearest_rank():
    assert ev.percentile([], 0.5) is None
    assert ev.percentile([5, 1, 3], 0.5) == 3
    assert ev.percentile(list(range(1, 11)), 0.9) == 9
    assert ev.percentile([7], 0.9) == 7


# --- fakes --------------------------------------------------------------------------------

def test_fake_supports_the_new_operators_and_unique_keys():
    coll = FakeColl(unique=[(("paymentId", "valueDate"), {"active": True})])
    coll.insert_one({"paymentId": "P", "valueDate": "d", "active": True, "n": 1})
    with pytest.raises(DuplicateKeyError):
        coll.insert_one({"paymentId": "P", "valueDate": "d", "active": True})
    coll.insert_one({"paymentId": "P", "valueDate": "d", "active": False})  # outside partial
    assert coll.count_documents({"n": {"$gt": 0}}) == 1
    assert coll.count_documents({"n": {"$exists": False}}) == 1
    assert coll.count_documents({"active": {"$nin": [False]}}) == 1
    assert coll.update_many({"paymentId": "P"}, {"$set": {"x": 1}}).matched_count == 2
    from pymongo import ReturnDocument
    after = coll.find_one_and_update({"active": True}, {"$set": {"n": 2}},
                                     return_document=ReturnDocument.AFTER)
    assert after["n"] == 2
    with pytest.raises(DuplicateKeyError):
        coll.find_one_and_update({"active": False}, {"$set": {"active": True}})


class _AggColl(FakeColl):
    def __init__(self, out):
        super().__init__()
        self.out, self.pipelines = out, []

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return iter(self.out)


def test_timing_aggregate_parses_the_facet_result():
    """The fakes cannot run `$percentile`; this pins the result parsing. The pipeline itself
    is checked at the C4 live gate against Atlas (MongoDB 7.0+)."""
    at = et(17, 5)
    db = FakeDB()
    db[ev.STAGE_EVENTS] = _AggColl([{
        "journeys": [{"_id": None, "n": 12, "p": [480_000, 1_200_000]}],
        "stage": [{"_id": None, "n": 30, "p": [300_000, 600_000]}],
        "mix": [{"_id": "SEED", "n": 40}, {"_id": None, "n": 2}],
        "live": [{"paymentId": "PAY-1", "meta": {"stage": "PENDING_APPROVAL"},
                  "durationMs": 120_000, "at": at}],
    }])
    out = ev._timing_aggregate(db, rail="WIRE", stages=["ROUTED"], stage="PENDING_APPROVAL", at=at)
    assert (out["remainingP50Min"], out["remainingP90Min"], out["journeySamples"]) == (8, 20, 12)
    assert (out["stageP50Min"], out["stageP90Min"], out["stageSamples"]) == (5, 10, 30)
    assert out["sourceMix"] == {"SEED": 40, "UNKNOWN": 2}
    assert out["liveRecent"] == [{"paymentId": "PAY-1", "stage": "PENDING_APPROVAL",
                                  "durationMin": 2, "at": at}]
    assert out["method"] == "$percentile"


def test_timing_aggregate_with_no_samples_has_no_percentiles():
    db = FakeDB()
    db[ev.STAGE_EVENTS] = _AggColl([{"journeys": [], "stage": [], "mix": [], "live": []}])
    out = ev._timing_aggregate(db, rail="WIRE", stages=["ROUTED"], stage="PENDING_FUNDS",
                               at=et(17, 5))
    assert out["remainingP50Min"] is None and out["stageP90Min"] is None
    assert out["journeySamples"] == 0 and out["sourceMix"] == {} and out["liveRecent"] == []
