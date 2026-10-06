"""C1d: the walkthrough's risk levels against the real seeded history (cutoff plan, scenario
arithmetic). Runs transactions `build_history` and the real staff directory; inputs match what
each scenario sets up (`cutoff_scenarios.SCENARIOS`).

C1 and C2 have thin margins after the internal cut-off. If a seed change flips one to
WILL_MISS here, shift the walkthrough's clock anchors (approved Q4) — do not loosen the rules.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

import cutoff_evidence as ev
import cutoff_risk as cr
import cutoff_window as cw
from tests import _transactions
from tests._fakes import FakeColl, FakeDB

SEED = _transactions.load("contexts/payment_orchestration/application/cutoff_seed.py",
                          "cutoff_seed")
DAY = date(2026, 10, 6)


def et(h, m):
    return datetime.combine(DAY, time(h, m), tzinfo=cw.ET).astimezone(timezone.utc)


@pytest.fixture(scope="module")
def db():
    d = FakeDB()
    d["paymentStageEvents"] = FakeColl(SEED.build_history(end_date=DAY))
    d["staffDirectory"] = FakeColl(SEED.staff_docs(DAY))
    d["accounts"] = FakeColl([SEED.funds_short_account_doc()])
    return d


def _screening(db, at, ahead):
    """The run's queue as the scenario builds it: `ahead` synthetic items, then the payment."""
    db["screeningQueue"] = FakeColl(
        [{"paymentId": f"SYN-{i}", "status": "OPEN", "priority": 0, "demo": {"clockRunId": "R"},
          "queuedAt": at - timedelta(minutes=5 * (ahead - i + 1))} for i in range(ahead)]
        + [{"paymentId": "PAY", "status": "OPEN", "priority": 0, "demo": {"clockRunId": "R"},
            "queuedAt": at - timedelta(minutes=1)}])
    return ev.screening_status(db, payment_id="PAY", at=at)


def _risk(db, *, status, wire_type, at, elapsed=0.0, screening=None, funds=None):
    timing = ev.stage_timing(db, rail="WIRE", status=status, at=at)
    return cr.assess(
        status=status, window=cw.window_for(rail="WIRE", wire_type=wire_type, currency="USD"),
        at=at, minutes_in_stage=elapsed, remaining_p50_min=timing["remainingP50Min"],
        remaining_p90_min=timing["remainingP90Min"], stage_p50_min=timing["stageP50Min"],
        stage_p90_min=timing["stageP90Min"], screening=screening, funds=funds)


def test_c1_before_internal_is_at_risk(db):
    r = _risk(db, status=cr.PENDING_APPROVAL, wire_type="DOMESTIC", at=et(17, 5))
    assert (r["phase"], r["riskLevel"]) == (cw.BEFORE_INTERNAL, cr.AT_RISK)


@pytest.mark.parametrize("minute", [16, 20])
def test_c1_after_approval_is_at_risk_not_will_miss(db, minute):
    r = _risk(db, status=cr.CUTOFF_EXCEPTION, wire_type="DOMESTIC", at=et(18, minute))
    assert (r["phase"], r["riskLevel"]) == (cw.AFTER_INTERNAL, cr.AT_RISK)
    assert r["remainingP90Min"] < r["minutesToExternal"]


def test_c2_will_miss_until_priority_is_raised(db):
    at = et(17, 50)
    queue = _screening(db, at, ahead=7)
    assert (queue["ahead"], queue["analystsOnShift"]) == (7, 1)
    before = _risk(db, status=cr.PENDING_SCREENING, wire_type="INTERNATIONAL", at=at,
                   screening=queue)
    assert before["riskLevel"] == cr.WILL_MISS
    after = _risk(db, status=cr.PENDING_SCREENING, wire_type="INTERNATIONAL", at=at,
                  screening={**queue, "ahead": 0})
    assert after["riskLevel"] == cr.AT_RISK


@pytest.mark.parametrize("minute", [16, 20])
def test_c2_after_internal_is_at_risk(db, minute):
    at = et(18, minute)
    screening = _risk(db, status=cr.PENDING_SCREENING, wire_type="INTERNATIONAL", at=at,
                      elapsed=minute + 10, screening={"ahead": 0, "analystsOnShift": 1})
    assert (screening["phase"], screening["riskLevel"]) == (cw.AFTER_INTERNAL, cr.AT_RISK)
    cleared = _risk(db, status=cr.CUTOFF_EXCEPTION, wire_type="INTERNATIONAL", at=at)
    assert cleared["riskLevel"] == cr.AT_RISK


def test_c3_funds_will_miss(db):
    at = et(16, 45)
    payment = {"debtor": {"accountId": SEED.FUNDS_SHORT_ACCOUNT},
               "instructedAmount": SEED.FUNDS_SHORT_AVAILABLE + 3_200.0}
    funds = ev.funds_position(db, payment, business_date=DAY)
    assert funds["shortfall"] == 3_200.0
    r = _risk(db, status=cr.PENDING_FUNDS, wire_type="DOMESTIC", at=at, funds=funds)
    assert (r["phase"], r["riskLevel"]) == (cw.BEFORE_INTERNAL, cr.WILL_MISS)


def test_c4_on_track(db):
    at = et(16, 34)
    queue = _screening(db, at, ahead=1)
    assert (queue["position"], queue["analystsOnShift"]) == (2, 3)
    r = _risk(db, status=cr.PENDING_SCREENING, wire_type="INTERNATIONAL", at=at, elapsed=4,
              screening=queue)
    assert (r["phase"], r["riskLevel"]) == (cw.BEFORE_INTERNAL, cr.ON_TRACK)


def test_c5_will_miss(db):
    r = _risk(db, status=cr.CUTOFF_EXCEPTION, wire_type="INTERNATIONAL", at=et(18, 35))
    assert r["minutesToExternal"] == 10.0
    assert (r["phase"], r["riskLevel"]) == (cw.AFTER_INTERNAL, cr.WILL_MISS)
