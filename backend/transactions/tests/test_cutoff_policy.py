"""Cutoff policy (cutoff plan A1). Pure: every input is explicit."""

from datetime import date, datetime, time, timedelta, timezone

import pytest

from contexts.payment_orchestration.domain import cutoff_policy as cp
from shared.business_clock import ET


def _et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(timezone.utc)


_WINTER, _SUMMER = date(2026, 1, 15), date(2026, 7, 15)


@pytest.mark.parametrize("business_date", [_WINTER, _SUMMER])
@pytest.mark.parametrize("wire_type, currency, internal, external", [
    ("DOMESTIC", "USD", (17, 30), (18, 45)),
    ("INTERNATIONAL", "USD", (18, 15), (18, 45)),
])
def test_four_cutoff_combinations(business_date, wire_type, currency, internal, external):
    window = cp.window_for(rail="WIRE", wire_type=wire_type, currency=currency)
    assert window is not None

    def phase_at(hm, delta):
        at = _et(business_date, *hm) + timedelta(minutes=delta)
        return cp.phase(window, at=at)

    assert phase_at(internal, -1) == cp.BEFORE_INTERNAL
    assert phase_at(internal, 0) == cp.BEFORE_INTERNAL
    assert phase_at(internal, +1) == cp.AFTER_INTERNAL
    assert phase_at(external, -1) == cp.AFTER_INTERNAL
    assert phase_at(external, 0) == cp.AFTER_INTERNAL
    assert phase_at(external, +1) == cp.AFTER_EXTERNAL


def test_cutoff_at_is_dst_correct():
    window = cp.window_for(rail="WIRE", wire_type="DOMESTIC", currency="USD")
    assert cp.cutoff_at(window, business_date=_WINTER, which="internal") == \
        datetime(2026, 1, 15, 22, 30, tzinfo=timezone.utc)
    assert cp.cutoff_at(window, business_date=_SUMMER, which="internal") == \
        datetime(2026, 7, 15, 21, 30, tzinfo=timezone.utc)
    assert cp.cutoff_at(window, business_date=_SUMMER, which="external") == \
        datetime(2026, 7, 15, 22, 45, tzinfo=timezone.utc)


def test_no_window_for_internal_or_non_usd_international():
    assert cp.window_for(rail="INTERNAL", wire_type=None, currency="USD") is None
    assert cp.window_for(rail="ACH", wire_type=None, currency="USD") is None
    assert cp.window_for(rail="WIRE", wire_type="INTERNATIONAL", currency="EUR") is None
    assert cp.window_for(rail="WIRE", wire_type=None, currency="USD") is None


def test_next_business_day_skips_weekends():
    # 2026-10-05 is a Monday.
    assert cp.next_business_day(date(2026, 10, 5)) == date(2026, 10, 6)
    assert cp.next_business_day(date(2026, 10, 8)) == date(2026, 10, 9)    # Thu -> Fri
    for weekendish in (date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 11)):
        assert cp.next_business_day(weekendish) == date(2026, 10, 12)       # -> Mon


def test_no_chips_row():
    assert set(cp._POLICY) == {"DOMESTIC", "INTERNATIONAL_USD"}
    for wire_type in ("DOMESTIC", "INTERNATIONAL"):
        window = cp.window_for(rail="WIRE", wire_type=wire_type, currency="USD")
        assert window.external_network != "CHIPS"
