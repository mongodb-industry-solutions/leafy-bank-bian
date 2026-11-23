"""Write simulated SWIFT gpi events and the `payments.tracking` summary (GPI plan §3).

Sync-only (decision 1): the events are written to `paymentEvents` and the tracker is the
query by UETR. There is no outbox and no update path — insert-only, like every other kind
in the collection.

## Idempotent by `_id`, not by catching the duplicate

`_id` is `{paymentId}:GPI:{seq}` and seq comes from the route, so a re-emitted step has the
same `_id` as the stored one. Inside a transaction a duplicate-key error aborts the whole
transaction, so `emit` reads which ids already exist and inserts only the rest; the unique
`_id` stays the server-side guard (a concurrent emitter write-conflicts and the transaction
retries, then sees the row). The stored event keeps its original timestamp.

Once a final status is stored the hop story is closed: forwards are not re-emitted, and a
*different* final status (a return after the ACCC, a credit after a refusal) is appended at
the next seq past everything stored instead of colliding with the stored final's `_id`. A RJCT
after a stored ACCC is a return by definition and is recorded as RETURNED.

## Timestamps

Strictly increasing per payment: forwards are `gpi.HOP_SPACING_SECONDS` apart from the first
report (`gpi.stamp`), a final status sits at `now`, and every new event is at least 1 ms after
the latest stored one — so the final report is never before the last hop, and a business clock
that jumps (either way) cannot reorder or collapse the story.

## Never breaks the money path

Tracking is a simulation layered on a payment that has already moved. `track` logs and
swallows any failure — the same rule as `business_clock`. A document-validation failure
means the live `paymentEvents` validator predates the GPI branch; that is logged once, loudly,
with the fix (`python -m data.ensure_indexes`). A payment without a UETR or a SWIFT
route (domestic, internal, legacy snapshot) is not tracked at all.
"""

from __future__ import annotations

import logging
import dataclasses
from datetime import datetime, timedelta
from typing import Optional

from pymongo.errors import BulkWriteError, OperationFailure

from contexts.payment_order_initiation.domain import lifecycle_history
from contexts.payment_rail.domain import gpi

logger = logging.getLogger(__name__)

_DOCUMENT_VALIDATION_FAILURE = 121
_MIN_GAP = timedelta(milliseconds=1)
_validation_failure_reported = False


def route_of(payments, payment: dict) -> list:
    """The route stage 4 recorded on the payment's routing snapshot; [] when there is none."""
    ref = (payment.get("refs") or {}).get("routingSnapshotId")
    if not ref:
        return []
    snapshot = payments.database["routingSnapshots"].find_one({"routingSnapshotId": ref})
    return list((snapshot or {}).get("route") or [])


def emit(payments, payment: dict, steps: list, *, now: datetime, direction: str,
         hops_total: int, base: Optional[datetime] = None, session=None) -> Optional[dict]:
    """Insert the steps not yet stored and refresh `payments.tracking`, in one transaction.

    Forwarding steps are stamped by `gpi.stamp` from `base`; a final status (ACCC/RJCT) and
    every step without `base` at `now`; each new event after the latest stored one. Returns the tracking summary.
    """
    history = lifecycle_history.collection(payments)
    ordered = sorted(steps, key=lambda st: st.seq)

    def at(step: gpi.Step) -> datetime:
        if base is None or step.gpi_status in gpi.FINAL_STATUSES:
            return now
        return gpi.stamp(base, step.seq - 1)

    def work(s):
        stored = list(history.find(
            {"paymentId": payment["paymentId"], "kind": gpi.KIND}, session=s))
        fresh = _fresh_documents(payment, ordered, stored, at=at, direction=direction,
                                 hops_total=hops_total)
        if fresh:
            history.insert_many(fresh, session=s)
        tracking = gpi.summary(stored + fresh, hops_total)
        payments.update_one({"paymentId": payment["paymentId"]},
                            {"$set": {"tracking": tracking}}, session=s)
        return tracking

    return lifecycle_history.run_in_transaction(payments, session, work)


def _fresh_documents(payment: dict, steps: list, stored: list, *, at, direction: str,
                     hops_total: int) -> list:
    """The event documents `steps` adds to `stored`: new seqs, closed after a final, spaced."""
    stored_ids = {d["_id"] for d in stored}
    finals = [d for d in stored if d["gpiStatus"] in gpi.FINAL_STATUSES]
    seen_finals = {(d["gpiStatus"], d.get("outcome")) for d in finals}
    credited = any(d["gpiStatus"] == gpi.ACCC for d in finals)
    next_seq = max([d["seq"] for d in stored] + [hops_total]) + 1
    last_at = max((d["at"] for d in stored), default=None)
    fresh = []
    for step in steps:
        if step.gpi_status not in gpi.FINAL_STATUSES:
            if finals or _event_id(payment, step.seq) in stored_ids:
                continue
        else:
            if step.gpi_status == gpi.RJCT and credited:
                step = dataclasses.replace(step, outcome=gpi.RETURNED)
            if (step.gpi_status, step.outcome) in seen_finals:
                continue
            if finals or _event_id(payment, step.seq) in stored_ids:
                step = dataclasses.replace(step, seq=next_seq)
                next_seq += 1
            seen_finals.add((step.gpi_status, step.outcome))
        stamped = at(step)
        if last_at is not None and stamped <= last_at:
            stamped = last_at + _MIN_GAP
        last_at = stamped
        fresh.append(gpi.event_document(payment, step, at=stamped, direction=direction))
    return fresh


def _event_id(payment: dict, seq: int) -> str:
    return f"{payment['paymentId']}:{gpi.KIND}:{seq}"


def track(payments, payment: Optional[dict], *, now: datetime, outcome: Optional[str] = None,
          first_only: bool = False, reject_at: Optional[int] = None,
          reason_code: Optional[str] = None, route: Optional[list] = None,
          direction: str = gpi.OUTBOUND, base: Optional[datetime] = None) -> None:
    """Emit the outbound script up to `outcome` for `payment`. Best-effort; never raises.

    `first_only` emits just the debtor agent's report (stage 5). `base` defaults to the time
    of that first report, so later hops read as spaced out after it.
    """
    try:
        if not payment or not payment.get("uetr"):
            return
        if route is None:
            route = route_of(payments, payment)
        if not route:
            return
        steps = gpi.script(route, outcome, reject_at=reject_at, reason_code=reason_code)
        if first_only:
            steps = steps[:1]
        if base is None:
            base = _first_report_at(payments, payment) or now
        emit(payments, payment, steps, now=now, direction=direction,
             hops_total=len(route), base=base)
    except Exception as exc:  # noqa: BLE001 - tracking must not fail a payment; see the docstring
        _report_failure(exc, (payment or {}).get("paymentId"))


def track_inbound(payments, payment: Optional[dict], *, now: datetime, outcome: str,
                  our_bic: str, our_country: str, reason_code: Optional[str] = None) -> None:
    """Our own final report on a received wire (decision 3): ACCC, or RJCT on a refusal."""
    try:
        if not payment or not payment.get("uetr"):
            return
        route = gpi.inbound_route(payment, our_bic, our_country)
        steps = [s for s in gpi.script(route, outcome, reason_code=reason_code)
                 if s.seq == len(route)]
        emit(payments, payment, steps, now=now, direction=gpi.INBOUND, hops_total=len(route))
    except Exception as exc:  # noqa: BLE001 - as `track`
        _report_failure(exc, (payment or {}).get("paymentId"))


def _first_report_at(payments, payment: dict) -> Optional[datetime]:
    first = lifecycle_history.collection(payments).find_one(
        {"_id": f"{payment['paymentId']}:{gpi.KIND}:1"})
    return (first or {}).get("at")


def _report_failure(exc: Exception, payment_id: Optional[str]) -> None:
    global _validation_failure_reported
    if not _is_validation_failure(exc):
        logger.exception("gpi tracking failed for %s", payment_id)
        return
    if not _validation_failure_reported:
        _validation_failure_reported = True
        logger.error(
            "gpi tracking DISABLED in effect: paymentEvents rejected a GPI event for %s with a "
            "document-validation error (code 121). The live validator predates the GPI branch. "
            "Fix: run `python -m data.ensure_indexes` from backend/transactions. Further "
            "validation failures are logged at DEBUG.", payment_id)
    else:
        logger.debug("gpi event rejected by the paymentEvents validator for %s", payment_id)


def _is_validation_failure(exc: Exception) -> bool:
    if isinstance(exc, BulkWriteError):
        return any(e.get("code") == _DOCUMENT_VALIDATION_FAILURE
                   for e in (exc.details or {}).get("writeErrors", []))
    return isinstance(exc, OperationFailure) and exc.code == _DOCUMENT_VALIDATION_FAILURE
