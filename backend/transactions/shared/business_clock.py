"""Business clock: the one place the money path asks "what time is it?".

An untagged payment gets real UTC. A payment tagged with a demo clock run (`demo.clockRunId`)
gets real UTC plus that run's `offsetSeconds`, read from `demoClocks`. That lets a presenter
put a payment at 17:31 ET without waiting for 17:31 ET, while every stage still reads one
consistent clock.

The clock never raises in the money path: a missing run, or a lookup that fails, falls back
to real time and logs a warning. A wrong demo time is a cosmetic failure; a refused payment
is not.

ET is DST-correct via `zoneinfo` for ALL traffic (cutoff plan, Decision 1). This replaced the
fixed −4h offset `orchestrate.py` used to carry.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from bson import ObjectId

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
CLOCKS = "demoClocks"

# A demo run's offset is fixed at creation, so a short cache only saves the round trip that
# every stage transition would otherwise make. Keyed per run id.
_CACHE_TTL_SECONDS = 5.0
_cache: dict[str, tuple[float, int]] = {}


def now(clock_run_id: Optional[str] = None, *, clocks=None) -> datetime:
    """Business time, tz-aware UTC."""
    real = datetime.now(timezone.utc)
    if not clock_run_id:
        return real
    offset = _offset_seconds(clock_run_id, clocks)
    return real + timedelta(seconds=offset) if offset is not None else real


def to_et(at: datetime) -> datetime:
    return at.astimezone(ET)


def minutes_since_midnight_et(at: datetime) -> int:
    et = to_et(at)
    return et.hour * 60 + et.minute


def anchor_offset(target_et_minutes: int, *, real_now: datetime,
                  on_date: Optional[date] = None) -> int:
    """The offset (seconds) that makes `real_now` read as `target_et_minutes` on `on_date`
    (default: today's ET date).

    Anchored to the start of the target minute, so a payment initiated a few seconds later
    still lands inside it. Rounded up so sub-second drift never lands in the minute before.
    """
    et = to_et(real_now)
    day = on_date or et.date()
    target = datetime(day.year, day.month, day.day, target_et_minutes // 60,
                      target_et_minutes % 60, tzinfo=ET)
    return math.ceil((target - et).total_seconds())


def create_run(clocks, *, offset_seconds: int, real_now: Optional[datetime] = None,
               scenario: Optional[str] = None, anchor_et_minutes: Optional[int] = None,
               business_date: Optional[date] = None) -> str:
    """Persist a clock run and return its id. `createdAt` feeds the 24h TTL index.

    The optional fields record what the run was anchored to, so a clock `reset` can return
    to the start minute (cutoff plan Q7) and a re-run can find its predecessors.
    """
    run_id = f"CLK-{ObjectId()}"
    doc = {
        "_id": run_id,
        "offsetSeconds": int(offset_seconds),
        "createdAt": real_now or datetime.now(timezone.utc),
    }
    if scenario is not None:
        doc["scenario"] = scenario
    if anchor_et_minutes is not None:
        doc["anchorEtMinutes"] = int(anchor_et_minutes)
    if business_date is not None:
        doc["businessDate"] = business_date.isoformat()
    clocks.insert_one(doc)
    return run_id


def forget(clock_run_id: str) -> None:
    """Drop one run's cached offset after its offset moves. Per process: other processes
    (the payment agent) see the move within the cache TTL."""
    _cache.pop(clock_run_id, None)


def clocks_for(ctx):
    """The `demoClocks` handle for a context, or None when the context has no database."""
    collections = getattr(ctx, "collections", None)
    db = getattr(collections, "db", None) if collections is not None else None
    return db[CLOCKS] if db is not None else None


def ctx_now(ctx) -> datetime:
    """Business time for a payment context. Duck-typed: reads `clock_run_id` and `collections`."""
    return now(getattr(ctx, "clock_run_id", None), clocks=clocks_for(ctx))


def _offset_seconds(clock_run_id: str, clocks) -> Optional[int]:
    cached = _cache.get(clock_run_id)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    if clocks is None:
        logger.warning("clock run %s requested without a demoClocks handle; using real time",
                       clock_run_id)
        return None
    try:
        run = clocks.find_one({"_id": clock_run_id})
    except Exception:  # noqa: BLE001 - never raise in the money path; see the module docstring
        logger.exception("could not read clock run %s; using real time", clock_run_id)
        return None
    if run is None:
        logger.warning("clock run %s not found; using real time", clock_run_id)
        return None
    offset = int(run.get("offsetSeconds") or 0)
    _cache[clock_run_id] = (time.monotonic(), offset)
    return offset


def _clear_cache() -> None:
    """Tests only."""
    _cache.clear()
