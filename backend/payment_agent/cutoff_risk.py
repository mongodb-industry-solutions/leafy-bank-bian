"""Cut-off risk for one held, tagged payment. Pure: no I/O, no clock reads.

The caller supplies business time, the window, and timing evidence (`cutoff_evidence`):
`remaining_*` is the journey still to run *after* the current stage; `stage_*` is the current
stage's own duration distribution. The current stage's residual is
`max(stage − elapsed, stage × 0.25)` — a stage never counts as "about to finish" — plus, for
screening, the queue ahead: `ahead × perItemP50 / max(1, analystsOnShift)`.

Risk levels, worst first:

- WILL_MISS: AFTER_EXTERNAL; or FUNDS with no EXPECTED credit covering the shortfall by the
  internal cut-off; or at CUTOFF_EXCEPTION `at + p90 > external`; otherwise `at + p50 > external`.
- AT_RISK: BEFORE_INTERNAL and `at + p90 > internal`; AFTER_INTERNAL is always at least
  AT_RISK — the internal cut-off has already been missed, so only an EXPEDITE gets it out today.
- ON_TRACK: otherwise.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from cutoff_window import (AFTER_EXTERNAL, AFTER_INTERNAL, CutoffWindow, cutoff_at,
                           phase as window_phase, to_et)

ON_TRACK = "ON_TRACK"
AT_RISK = "AT_RISK"
WILL_MISS = "WILL_MISS"

PENDING_APPROVAL = "PENDING_APPROVAL"
PENDING_FUNDS = "PENDING_FUNDS"
PENDING_SCREENING = "PENDING_SCREENING"
CUTOFF_EXCEPTION = "CUTOFF_EXCEPTION"
PENDING_HOLDS = (PENDING_APPROVAL, PENDING_FUNDS, PENDING_SCREENING)

BLOCKER_FOR = {
    PENDING_APPROVAL: "APPROVAL",
    PENDING_FUNDS: "FUNDS",
    PENDING_SCREENING: "SCREENING",
    CUTOFF_EXCEPTION: "CUTOFF_DECISION",
}

RESIDUAL_FLOOR = 0.25


def stage_residual(stage_min: Optional[float], elapsed_min: float) -> float:
    if not stage_min:
        return 0.0
    return max(stage_min - elapsed_min, stage_min * RESIDUAL_FLOOR)


def queue_minutes(screening: Optional[dict], per_item_default: Optional[float]) -> float:
    if not screening:
        return 0.0
    ahead = int(screening.get("ahead") or 0)
    per_item = screening.get("perItemP50Min")
    per_item = per_item if per_item is not None else (per_item_default or 0.0)
    return ahead * per_item / max(1, int(screening.get("analystsOnShift") or 0))


def funds_covered(funds: Optional[dict], *, by: datetime) -> bool:
    """True when there is no shortfall, or an EXPECTED credit covers it by `by`."""
    shortfall = float((funds or {}).get("shortfall") or 0)
    if shortfall <= 0:
        return True
    return any(
        c.get("status") == "EXPECTED" and float(c.get("amount") or 0) >= shortfall
        and c.get("expectedAt") is not None and c["expectedAt"] <= by
        for c in (funds or {}).get("expectedCredits") or []
    )


def _minutes(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 60, 1)


def assess(*, status: str, window: Optional[CutoffWindow], at: datetime,
           minutes_in_stage: float, remaining_p50_min: Optional[float],
           remaining_p90_min: Optional[float], stage_p50_min: Optional[float] = None,
           stage_p90_min: Optional[float] = None, screening: Optional[dict] = None,
           funds: Optional[dict] = None, open_exceptions: int = 0) -> Optional[dict]:
    """The risk record for one payment at business time `at`; None when no window applies."""
    if window is None:
        return None
    day = to_et(at).date()
    internal = cutoff_at(window, business_date=day, which="internal")
    external = cutoff_at(window, business_date=day, which="external")
    current = window_phase(window, at=at)
    elapsed = max(0.0, float(minutes_in_stage or 0))

    queue = queue_minutes(screening, stage_p50_min) if status == PENDING_SCREENING else 0.0
    downstream_p50 = float(remaining_p50_min or 0)
    downstream_p90 = float(remaining_p90_min or 0)
    p50 = round(stage_residual(stage_p50_min, elapsed) + queue + downstream_p50, 1)
    p90 = round(stage_residual(stage_p90_min, elapsed) + queue + downstream_p90, 1)
    done_p50 = at + timedelta(minutes=p50)
    done_p90 = at + timedelta(minutes=p90)

    reasons = []
    if remaining_p50_min is None or remaining_p90_min is None:
        reasons.append("No timing samples for the remaining journey.")
    level = ON_TRACK
    if current == AFTER_EXTERNAL:
        level = WILL_MISS
        reasons.append("Past the external cut-off.")
    elif status == PENDING_FUNDS and not funds_covered(funds, by=internal):
        level = WILL_MISS
        reasons.append("No expected credit covers the shortfall before the internal cut-off.")
    elif status == CUTOFF_EXCEPTION and done_p90 > external:
        level = WILL_MISS
        reasons.append(f"p90 {p90} min overruns the external cut-off.")
    elif status != CUTOFF_EXCEPTION and done_p50 > external:
        level = WILL_MISS
        reasons.append(f"p50 {p50} min overruns the external cut-off.")
    elif current == AFTER_INTERNAL:
        level = AT_RISK
        reasons.append("Past the internal cut-off; only an expedite makes today.")
    elif done_p90 > internal:
        level = AT_RISK
        reasons.append(f"p90 {p90} min overruns the internal cut-off.")

    release_p90 = at + timedelta(minutes=downstream_p90)
    return {
        "at": at,
        "phase": current,
        "riskLevel": level,
        "wireType": window.wire_type,
        "internalCutoffAt": internal,
        "externalCutoffAt": external,
        "minutesToInternal": _minutes(at, internal),
        "minutesToExternal": _minutes(at, external),
        "blockerType": BLOCKER_FOR.get(status),
        "remainingP50Min": p50,
        "remainingP90Min": p90,
        "queueMinutes": round(queue, 1),
        "openExceptions": int(open_exceptions or 0),
        "readiness": {
            "releasedNowP90Min": round(downstream_p90, 1),
            "makesInternal": release_p90 <= internal,
            "makesExternal": release_p90 <= external,
        },
        "reasons": reasons,
    }
