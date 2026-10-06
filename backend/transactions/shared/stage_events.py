"""Stage-duration events for tagged payments (cutoff plan A3) -> `paymentStageEvents`.

One point per transition: how long the payment sat in the stage it just left. Tagged payments
only — they are the cutoff agent's subjects, and untagged second-scale durations would skew the
seeded ops-time percentiles. Best-effort: logs, never raises.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STAGE_EVENTS = "paymentStageEvents"


def _utc(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def append(coll, updated_payment: dict, *, at: datetime) -> None:
    try:
        events = (updated_payment.get("lifecycle") or {}).get("events") or []
        if len(events) < 2:
            return
        prev, current = events[-2], events[-1]
        at = _utc(at)
        duration = (at - _utc(prev["at"])).total_seconds() * 1000
        coll.insert_one({
            "at": at,
            "meta": {
                "rail": updated_payment.get("rail"),
                "segment": (updated_payment.get("entitlement") or {}).get("segment"),
                "stage": prev.get("state"),
            },
            "toState": current.get("state"),
            "durationMs": max(0, int(duration)),
            "paymentId": updated_payment.get("paymentId"),
            "wireType": (updated_payment.get("wireDetails") or {}).get("wireType"),
            "clockRunId": (updated_payment.get("demo") or {}).get("clockRunId"),
            "source": "LIVE",
        })
    except Exception:  # noqa: BLE001 - best-effort telemetry; never breaks a transition
        logger.exception("could not append stage event for %s",
                         updated_payment.get("paymentId"))
