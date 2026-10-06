"""Business time for a demo clock run, read uncached.

The transactions clock caches a run's offset for 5 s (R5). The agent reads `demoClocks` on
every call instead, so a presenter's `move_clock` is visible on the agent's next look. A run
that is gone (24 h TTL, or deleted) yields None: the case built on it has expired.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from cutoff_window import to_et

CLOCKS = "demoClocks"


def run(db: Any, run_id: Optional[str]) -> Optional[dict]:
    if not run_id:
        return None
    return db[CLOCKS].find_one({"_id": run_id})


def now(db: Any, run_id: Optional[str], *, real_now: Optional[datetime] = None) -> Optional[datetime]:
    """Real UTC plus the run's offset, or None when the run does not exist."""
    doc = run(db, run_id)
    if doc is None:
        return None
    real = real_now or datetime.now(timezone.utc)
    return real + timedelta(seconds=int(doc.get("offsetSeconds") or 0))


def business_date(run_doc: Optional[dict], at: datetime) -> date:
    """The run's recorded business date (weekend rehearsals pin Friday), else `at` in ET."""
    recorded = (run_doc or {}).get("businessDate")
    return date.fromisoformat(recorded) if recorded else to_et(at).date()
