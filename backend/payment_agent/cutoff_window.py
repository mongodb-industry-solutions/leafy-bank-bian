"""Cut-off windows for tagged wires — a mirror of transactions `cutoff_policy`.

payment_agent cannot import transactions code, so this copies the policy table and its
phase semantics. `test_cutoff_core.py` loads the transactions module by file path and pins
parity; change both together.

Keyed by wire type, not by the routed network: there is no CHIPS row (cutoff plan G1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

BEFORE_INTERNAL = "BEFORE_INTERNAL"
AFTER_INTERNAL = "AFTER_INTERNAL"
AFTER_EXTERNAL = "AFTER_EXTERNAL"

Phase = Literal["BEFORE_INTERNAL", "AFTER_INTERNAL", "AFTER_EXTERNAL"]


@dataclass(frozen=True)
class CutoffWindow:
    wire_type: str
    internal_minutes_et: int
    external_minutes_et: int
    external_network: str


_EXTERNAL_NETWORK = "FEDWIRE"

# (internal, external) cut-off, minutes since midnight ET.
_POLICY = {
    "DOMESTIC": (17 * 60 + 30, 18 * 60 + 45),
    "INTERNATIONAL_USD": (18 * 60 + 15, 18 * 60 + 45),
}


def to_et(at: datetime) -> datetime:
    return at.astimezone(ET)


def minutes_since_midnight_et(at: datetime) -> int:
    et = to_et(at)
    return et.hour * 60 + et.minute


def window_for(*, rail: Optional[str], wire_type: Optional[str],
               currency: Optional[str]) -> Optional[CutoffWindow]:
    """The cut-off window for a payment, or None when no cut-off applies."""
    if (rail or "").upper() != "WIRE":
        return None
    key = _policy_key(wire_type, currency)
    if key is None:
        return None
    internal, external = _POLICY[key]
    return CutoffWindow(key, internal, external, _EXTERNAL_NETWORK)


def window_for_payment(payment: dict) -> Optional[CutoffWindow]:
    """Same lookup as transactions `PaymentsService._cutoff_window`."""
    return window_for(
        rail=payment.get("rail"),
        wire_type=(payment.get("wireDetails") or {}).get("wireType"),
        currency=payment.get("currency") or payment.get("instructedCurrency"),
    )


def phase(window: CutoffWindow, *, at: datetime) -> Phase:
    """Where `at` falls against the window. A cut-off minute itself is still in time."""
    minutes = minutes_since_midnight_et(at)
    if minutes > window.external_minutes_et:
        return AFTER_EXTERNAL
    if minutes > window.internal_minutes_et:
        return AFTER_INTERNAL
    return BEFORE_INTERNAL


def cutoff_at(window: CutoffWindow, *, business_date: date,
              which: Literal["internal", "external"]) -> datetime:
    """The cut-off instant on `business_date`, as tz-aware UTC (DST-correct)."""
    minutes = window.internal_minutes_et if which == "internal" else window.external_minutes_et
    local = datetime.combine(business_date, time(minutes // 60, minutes % 60), tzinfo=ET)
    return local.astimezone(timezone.utc)


def next_business_day(d: date) -> date:
    """The next weekday after `d`. Fri, Sat and Sun all roll to Monday. No holiday calendar."""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def _policy_key(wire_type: Optional[str], currency: Optional[str]) -> Optional[str]:
    kind = (wire_type or "").upper()
    if kind == "DOMESTIC":
        return "DOMESTIC"
    if kind == "INTERNATIONAL" and (currency or "").upper() == "USD":
        return "INTERNATIONAL_USD"
    return None
