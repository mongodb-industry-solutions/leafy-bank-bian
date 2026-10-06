"""Cutoff plan Part B: the seeded world `cutoff_seed` builds (pure, no database).

The stage-duration history has to support the agent's claims in the scenarios: screening
slows after 17:00 (one analyst), C4's queue p90 is about 9 minutes, and C5's remaining work
after 18:00 exceeds its 10 minutes to Fedwire.
"""

from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import quantiles

import pytest

from contexts.payment_orchestration.application import cutoff_seed
from shared import business_clock, hold_queues

TUESDAY = date(2026, 10, 6)


def _et(day, hhmm):
    return cutoff_seed._et(day, hhmm)


def _p90(values):
    return quantiles(values, n=10)[8]


def _entered_minute(point):
    entered = point["at"] - timedelta(milliseconds=point["durationMs"])
    return business_clock.minutes_since_midnight_et(entered)


@pytest.fixture(scope="module")
def history():
    return cutoff_seed.build_history(end_date=TUESDAY)


def _screening(history, *, late):
    return [p["durationMs"] / 60000 for p in history
            if p["meta"]["stage"] == "PENDING_SCREENING"
            and (_entered_minute(p) >= 17 * 60) == late]


# --- staff ----------------------------------------------------------------------

def test_staff_story():
    staff = {s["staffId"]: s for s in cutoff_seed.staff_docs(TUESDAY)}
    at_1816 = _et(TUESDAY, "18:16")

    maya = staff[cutoff_seed.STAFF_MAYA]
    assert maya["primaryFor"] == [cutoff_seed.ABC_ACCOUNT]
    assert maya["customerId"] == cutoff_seed.MAYA
    assert not hold_queues.on_shift(maya, at_1816)
    assert maya["outOfOfficeUntil"] == _et(date(2026, 10, 7), "09:00")

    raj = staff[cutoff_seed.STAFF_RAJ]
    assert raj["backupFor"] == cutoff_seed.STAFF_MAYA
    assert raj["customerId"] == cutoff_seed.RAJ
    assert hold_queues.on_shift(raj, at_1816)
    assert hold_queues.on_shift(staff[cutoff_seed.STAFF_LENA], at_1816)
    assert all(s["sourceSystem"] == cutoff_seed.SOURCE_SYSTEM for s in staff.values())


def test_friday_ooo_runs_to_monday():
    maya = cutoff_seed.staff_docs(date(2026, 10, 9))[0]
    assert maya["outOfOfficeUntil"] == _et(date(2026, 10, 12), "09:00")


@pytest.mark.parametrize("hhmm,expected", [("16:30", 3), ("17:00", 1), ("17:50", 1),
                                           ("20:59", 1), ("21:00", 0)])
def test_one_analyst_after_17(hhmm, expected):
    analysts = [s for s in cutoff_seed.staff_docs(TUESDAY) if s["role"] == "ANALYST"]
    assert sum(hold_queues.on_shift(s, _et(TUESDAY, hhmm)) for s in analysts) == expected


def test_lena_has_six_open_synthetic_requests():
    requests = cutoff_seed.synthetic_lena_requests(TUESDAY)

    assert len(requests) == 6
    assert {r["assignedTo"] for r in requests} == {cutoff_seed.STAFF_LENA}
    assert all(r["status"] == hold_queues.OPEN and r["synthetic"] for r in requests)
    assert len({r["paymentId"] for r in requests}) == 6
    assert all(r["requestedAt"] < _et(TUESDAY, "17:00") for r in requests)


# --- accounts -------------------------------------------------------------------

def test_funds_short_account_and_expected_credit():
    account = cutoff_seed.funds_short_account_doc()

    assert account["accountId"] == cutoff_seed.FUNDS_SHORT_ACCOUNT
    assert account["balance"]["available"] == 6_300.0
    assert account["customerSnapshot"]["customerId"] == cutoff_seed.ABC_OWNER
    assert [s["customerId"] for s in account["signatories"]] == [cutoff_seed.ABC_OWNER]
    [credit] = account["expectedCredits"]
    assert (credit["amount"], credit["expectedAtEt"], credit["status"]) == \
        (4_000.0, "17:45", "EXPECTED")
    # C3 = available + 3,200 must stay at or under the 10,000 single-approval line (Q5).
    assert account["balance"]["available"] + 3_200 <= 10_000


def test_approvers_join_abc_as_joint_signatories():
    assert [(s["customerId"], s["signingRule"]) for s in cutoff_seed.ABC_SIGNATORIES] == [
        (cutoff_seed.MAYA, "JOINT"), (cutoff_seed.RAJ, "JOINT")]


# --- history --------------------------------------------------------------------

def test_history_size_window_and_weekdays(history):
    assert 3_000 <= len(history) <= 5_000
    days = {business_clock.to_et(p["at"] - timedelta(milliseconds=p["durationMs"])).date()
            for p in history}
    assert all(d.weekday() < 5 for d in days)
    assert min(days) >= TUESDAY - timedelta(days=14)
    assert len(days) == 11, "10 weekdays + today"
    today = [p for p in history if business_clock.to_et(p["at"]).date() == TUESDAY]
    assert today and max(p["at"] for p in today) <= _et(TUESDAY, "18:45")


def test_history_points_are_seed_shaped(history):
    point = history[0]
    assert point["source"] == "SEED"
    assert point["seedBatch"] == TUESDAY.isoformat()
    assert point["paymentId"].startswith("HIST-")
    assert point["clockRunId"] is None
    assert set(point["meta"]) == {"rail", "segment", "stage"}
    assert point["at"].tzinfo is not None


def test_history_is_deterministic(history):
    assert cutoff_seed.build_history(end_date=TUESDAY) == history
    assert cutoff_seed.build_history(end_date=TUESDAY, seed=1) != history


def test_screening_p90_is_higher_after_17(history):
    assert _p90(_screening(history, late=True)) > _p90(_screening(history, late=False))


def test_c4_screening_p90_is_about_nine_minutes(history):
    assert 6.0 <= _p90(_screening(history, late=False)) <= 12.0


def test_c5_remaining_work_p90_exceeds_ten_minutes(history):
    remaining, late = defaultdict(float), set()
    for p in history:
        if p["meta"]["stage"] in ("ROUTED", "AUTHORISED", "APPROVED"):
            remaining[p["paymentId"]] += p["durationMs"] / 60000
        if p["meta"]["stage"] == "ROUTED" and _entered_minute(p) >= 18 * 60:
            late.add(p["paymentId"])

    assert len(late) >= 10
    assert _p90([remaining[i] for i in late]) > 10


def test_seed_never_touches_payments(history):
    ids = {p["paymentId"] for p in history}
    ids |= {r["paymentId"] for r in cutoff_seed.synthetic_lena_requests(TUESDAY)}
    assert all(i.startswith(("HIST-", "SYN-")) for i in ids)
    assert not any("PAY-" in i for i in ids)
    loader = (Path(__file__).resolve().parents[2] / "data" / "load_cutoff_seed.py").read_text()
    assert '"payments"' not in loader and "db.payments" not in loader


def test_business_date_rolls_back_at_the_weekend():
    assert cutoff_seed.business_date_for(_et(date(2026, 10, 10), "12:00")) == date(2026, 10, 9)
    assert cutoff_seed.business_date_for(_et(TUESDAY, "23:30")) == TUESDAY
