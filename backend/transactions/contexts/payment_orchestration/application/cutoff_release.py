"""Cutoff release: every `CUTOFF-DEMO-` payment ends the day closed, not parked.

A demo wire stops at its hold, and nothing in the running system ever resumes it. This module
is the one code path that does: it fast-forwards the run's clock to the next business day's
open and drives each held payment through the real `PaymentsService` methods — the same ones
Raj, the analyst and the cut-off desk use. No new ledger code: the ledger never reads
`payments`, and money still moves only in stage 5's ACID block.

Two triggers share it: `POST /workflow/demo/cutoff-release` (the presenter's button) and the
janitor worker (`sweep_stale`, abandoned runs).

## Who acts

Every deployment's workers share one database, so a claim on the payment decides who acts,
not a process lock: `payments.demo.release = {state, by, at, leaseUntil, attempts}` is taken
with one compare-and-set write. A live lease means someone else is working; a DONE payment is
finished; three attempts end the retries. `_held_payment`'s status check is the second guard.

## What it never touches

Only payments with both `demo.clockRunId` and a `CUTOFF-DEMO-` client reference. And never
`cutoff.decision`: the recorded decision stays, so the case outcome stays HELD / DEFERRED.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from contexts.fraud_evaluation.domain import sanctions
from contexts.payment_order_initiation.domain import lifecycle
from contexts.payment_orchestration.application import cutoff_scenarios, cutoff_seed
from contexts.payment_orchestration.domain import cutoff_policy
from shared import business_clock

logger = logging.getLogger(__name__)

ROUTE_ACTOR = "cutoff-release"
JANITOR_ACTOR = "cutoff-release-janitor"
NEXT_DAY_OPEN = "09:00"

CLAIMED = "CLAIMED"
DONE = "DONE"
FAILED = "FAILED"
MAX_ATTEMPTS = 3
LEASE_SECONDS = 120
# A chain like screening clear -> HOLD -> ROUTED -> release is three steps; one spare.
MAX_STEPS = 4

HOLD = "HOLD_NEXT_VALUE_DATE"
NEXT_VALUE_DATE = "NEXT_VALUE_DATE"

CLEAR_SCREENING = "CLEAR_SCREENING"
CREDIT_AND_RECHECK = "CREDIT_AND_RECHECK"
APPROVE = "APPROVE"
EXPEDITE = "EXPEDITE"
RELEASE = "RELEASE"
SKIP_IN_FLIGHT = "SKIP_IN_FLIGHT"
SKIP_REVIEW = "SKIP_REVIEW"
SKIP_TERMINAL = "SKIP_TERMINAL"
SKIP_UNKNOWN = "SKIP_UNKNOWN"
ALREADY_DONE = "ALREADY_DONE"
CLAIMED_ELSEWHERE = "CLAIMED_ELSEWHERE"
ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"

_IN_FLIGHT = frozenset({lifecycle.AUTHORISED, lifecycle.APPROVED, lifecycle.SUBMITTED,
                        lifecycle.IN_PROGRESS, lifecycle.POSTED})
_TERMINAL = frozenset({lifecycle.SETTLED, lifecycle.RECONCILED, lifecycle.REJECTED,
                       lifecycle.RETURNED, lifecycle.CANCELLED, lifecycle.FAILED,
                       lifecycle.REVERSED, lifecycle.REFUNDED})
# What the janitor looks for: the statuses a release can still act on.
OPEN_STATUSES = (lifecycle.PENDING_APPROVAL, lifecycle.PENDING_FUNDS,
                 lifecycle.PENDING_SCREENING, lifecycle.CUTOFF_EXCEPTION, lifecycle.ROUTED)


class ReleaseBusy(Exception):
    """Every payment that needed releasing is claimed by another worker. The route's 409."""


@dataclass(frozen=True)
class Plan:
    action: str
    next_day: bool = False

    @property
    def skip(self) -> bool:
        return self.action.startswith("SKIP")


def plan_for(payment: dict, *, phase: Optional[str]) -> Plan:
    """What release does next for a payment at its current state. Pure.

    `phase` is the payment's cut-off phase on the run's current business clock, or None when
    no cut-off window applies. It only matters for a screening hold: one that is still in
    time with no decision recorded is the on-track story, cleared the same day.
    """
    status = payment.get("status")
    decision = (payment.get("cutoff") or {}).get("decision")
    if status in _TERMINAL:
        return Plan(SKIP_TERMINAL)
    if status in _IN_FLIGHT:
        return Plan(SKIP_IN_FLIGHT)
    if status == lifecycle.MANUAL_FRAUD_REVIEW:
        return Plan(SKIP_REVIEW)
    if status == lifecycle.PENDING_SCREENING:
        on_track = decision is None and phase in (None, cutoff_policy.BEFORE_INTERNAL)
        return Plan(CLEAR_SCREENING, next_day=not on_track)
    if status == lifecycle.PENDING_FUNDS:
        return Plan(CREDIT_AND_RECHECK, next_day=True)
    if status == lifecycle.PENDING_APPROVAL:
        return Plan(APPROVE, next_day=True)
    if status == lifecycle.CUTOFF_EXCEPTION:
        return Plan(EXPEDITE, next_day=True)
    if status == lifecycle.ROUTED and decision in (HOLD, NEXT_VALUE_DATE):
        return Plan(RELEASE, next_day=True)
    return Plan(SKIP_UNKNOWN)


# --- the funds credit -----------------------------------------------------------

def credit_expected_funds(service, payment: dict, *, real_now: Optional[datetime] = None) -> float:
    """Land the credit the seed's story promises, once per payment. Returns the amount
    credited, or 0.0 when this payment already received it.

    A direct balance credit on the seed-owned account, shaped like `execute._money_move`'s
    `$inc`. It writes no `transactions` document and so no ledger event — the same accepted
    gap as the seed's opening balance. `expectedCredits[]` stays untouched so every run
    retells the story. One atomic update keyed on `demoCredits.paymentId $ne` makes a retry
    harmless; `sourceSystem` keeps it off any account this seed does not own.
    """
    now = real_now or datetime.now(timezone.utc)
    payment_id = payment["paymentId"]
    account_id = (payment.get("debtor") or {}).get("accountId")
    account = service.accounts.find_one({"accountId": account_id}) or {}
    if account.get("sourceSystem") != cutoff_seed.SOURCE_SYSTEM:
        raise ValueError(f"Account {account_id} is not owned by the cutoff demo seed; "
                         "refusing to credit it.")
    available = cutoff_scenarios._as_float((account.get("balance") or {}).get("available"))
    shortfall = cutoff_scenarios._as_float(payment.get("instructedAmount")) - available
    amount = round(max(cutoff_seed.EXPECTED_CREDIT["amount"], shortfall), 2)
    credited = service.accounts.find_one_and_update(
        {"accountId": account_id, "sourceSystem": cutoff_seed.SOURCE_SYSTEM, "type": "CURRENT",
         "demoCredits.paymentId": {"$ne": payment_id}},
        {
            "$inc": {"balance.current": amount, "balance.available": amount,
                     "balance.ledger": amount},
            "$set": {"balance.updatedAt": now, "updatedAt": now},
            "$push": {"demoCredits": {"paymentId": payment_id, "amount": amount, "at": now,
                                      "reason": cutoff_seed.EXPECTED_CREDIT["reference"]}},
        },
    )
    return amount if credited is not None else 0.0


# --- the payment claim ----------------------------------------------------------

def _claim(payments, payment: dict, *, actor: str, now: datetime) -> Optional[str]:
    """Take the payment's release claim. None on success, else why not (a result action)."""
    release = (payment.get("demo") or {}).get("release") or {}
    if release.get("state") == DONE:
        return ALREADY_DONE
    attempts = release.get("attempts")
    if (attempts or 0) >= MAX_ATTEMPTS:
        return ATTEMPTS_EXHAUSTED
    claimed = payments.find_one_and_update(
        {
            "_id": payment["_id"],
            # Compare-and-set on the attempts we read: a rival's claim bumps it.
            "demo.release.attempts": attempts,
            "demo.release.state": {"$ne": DONE},
            "$or": [{"demo.release.leaseUntil": {"$exists": False}},
                    {"demo.release.leaseUntil": {"$lte": now}}],
        },
        {"$set": {"demo.release": {
            "state": CLAIMED, "by": actor, "at": now,
            "leaseUntil": now + timedelta(seconds=LEASE_SECONDS),
            "attempts": (attempts or 0) + 1,
        }}},
    )
    return None if claimed is not None else CLAIMED_ELSEWHERE


def _finish(payments, payment_id, *, state: str, result: dict, now: datetime) -> None:
    # `leaseUntil` is set to now rather than removed: an expired lease is what lets a
    # FAILED payment be claimed again.
    payments.update_one({"_id": payment_id}, {"$set": {
        "demo.release.state": state, "demo.release.finishedAt": now,
        "demo.release.leaseUntil": now, "demo.release.result": result,
    }})


# --- release --------------------------------------------------------------------

def _phase(payment: dict, now: datetime) -> Optional[str]:
    window = cutoff_policy.window_for(
        rail=payment.get("rail"),
        wire_type=(payment.get("wireDetails") or {}).get("wireType"),
        currency=payment.get("currency") or payment.get("instructedCurrency"),
    )
    return cutoff_policy.phase(window, at=now) if window is not None else None


def _roll_value_date_forward(service, payment: dict) -> None:
    """Move a past value date to the run's current business date.

    Capture stamps `requestedExecutionDate` and stage 4a snapshots the value date from it, so
    an undecided payment released after the clock moved would settle with yesterday's date.
    `demo.release.valueDate` is what `_context_from_doc` turns into the resume override. A
    HOLD / NEXT_VALUE_DATE payment already carries a future date and is left alone, and
    `cutoff.decision` is never written.
    """
    requested = payment.get("requestedExecutionDate")
    clocks = service.db[business_clock.CLOCKS]
    today = business_clock.to_et(
        business_clock.now(payment["demo"]["clockRunId"], clocks=clocks)).date().isoformat()
    if requested and requested < today:
        service.payments.update_one({"_id": payment["_id"]}, {"$set": {
            "requestedExecutionDate": today, "demo.release.valueDate": today}})


def _execute(service, plan: Plan, payment: dict, *, actor: str, now: datetime) -> dict:
    payment_id = payment["paymentId"]
    if plan.action != RELEASE:
        _roll_value_date_forward(service, payment)
    if plan.action == CLEAR_SCREENING:
        return service.resolve_screening(payment_id, analyst_id=cutoff_seed.STAFF_ANALYST_3,
                                         outcome=sanctions.CLEAR)
    if plan.action == APPROVE:
        # Maya is back from leave at the next business day's open.
        return service.approve_payment(payment_id, approver_id=cutoff_seed.MAYA,
                                       decision="APPROVED")
    if plan.action == EXPEDITE:
        return service.decide_cutoff(payment_id, decision="EXPEDITE", decided_by=actor)
    if plan.action == RELEASE:
        return service.release_warehoused(payment_id, released_by=actor)
    credit_expected_funds(service, payment, real_now=now)
    updated, short_by = service.recheck_funds(payment_id)
    if short_by:
        raise ValueError(f"Payment {payment_id} is still short by {short_by:,.2f} after the "
                         "credit.")
    return updated


def release_payment(service, payment: dict, *, actor: str, move_clock,
                    real_now: Optional[datetime] = None) -> dict:
    """Claim one payment and drive it as far as it will go. Returns its result row:
    `{paymentId, before, action, after, error?}`.

    `move_clock` is a no-argument callable that fast-forwards the run (idempotent); it is
    called once, before the first step that needs the next business day. A payment with
    nothing to do is reported without a claim, so it carries no `demo.release`.
    """
    real_now = real_now or datetime.now(timezone.utc)
    payments = service.payments
    payment_id = payment["paymentId"]
    run_id = payment["demo"]["clockRunId"]
    clocks = service.db[business_clock.CLOCKS]
    result = {"paymentId": payment_id, "before": payment["status"], "action": None,
              "after": payment["status"]}

    first = plan_for(payment, phase=_phase(payment, business_clock.now(run_id, clocks=clocks)))
    if first.skip:
        result["action"] = first.action
        return result
    refused = _claim(payments, payment, actor=actor, now=real_now)
    if refused:
        result["action"] = refused
        return result

    actions: list[str] = []
    error = None
    try:
        for _ in range(MAX_STEPS):
            current = payments.find_one({"_id": payment["_id"]})
            plan = plan_for(current, phase=_phase(
                current, business_clock.now(run_id, clocks=clocks)))
            if plan.skip:
                break
            if plan.next_day:
                move_clock()
            actions.append(plan.action)
            updated = _execute(service, plan, current,
                               actor=actor, now=real_now)
            result["after"] = updated["status"]
            if updated["status"] == current["status"]:
                raise ValueError(f"Payment {payment_id} did not leave {current['status']}.")
    except Exception as exc:  # noqa: BLE001 - one payment's failure must not stop the run
        logger.exception("cutoff release: %s failed", payment_id)
        error = str(exc)
    result["action"] = "+".join(actions) or None
    result["after"] = (payments.find_one({"_id": payment["_id"]}) or {}).get("status")
    if error:
        result["error"] = error
    _finish(payments, payment["_id"], state=FAILED if error else DONE, result=dict(result),
            now=datetime.now(timezone.utc))
    return result


def _tagged_payments(service, run_id: str) -> list[dict]:
    return [p for p in service.payments.find({"demo.clockRunId": run_id})
            if str(p.get("clientReference") or "").startswith(cutoff_scenarios.CLIENT_REF_PREFIX)]


def release_run(service, run_id: str, *, actor: str,
                real_now: Optional[datetime] = None) -> dict:
    """Release every held `CUTOFF-DEMO-` payment on one clock run.

    Returns `{runId, clock, payments: [result rows]}`. Raises LookupError for an unknown run
    and `ReleaseBusy` when payments needed releasing but another worker holds every claim.
    Re-running is safe: closed payments are skipped and a released one is not claimed again.
    """
    real_now = real_now or datetime.now(timezone.utc)
    db = service.db
    run = db[business_clock.CLOCKS].find_one({"_id": run_id})
    if run is None:
        raise LookupError(f"Clock run {run_id} not found.")

    def move() -> dict:
        return cutoff_scenarios.move_clock(
            db, run_id, next_business_day=NEXT_DAY_OPEN, real_now=real_now)

    rows = [release_payment(service, p, actor=actor, move_clock=move, real_now=real_now)
            for p in _tagged_payments(service, run_id)]
    busy = [r for r in rows if r["action"] == CLAIMED_ELSEWHERE]
    if busy and len(busy) == len(rows):
        raise ReleaseBusy(f"Run {run_id} is being released by another worker.")
    fresh = db[business_clock.CLOCKS].find_one({"_id": run_id}) or run
    return {
        "runId": run_id,
        "clock": cutoff_scenarios._clock_view(
            run_id, int(fresh.get("offsetSeconds") or 0), real_now=real_now),
        "payments": rows,
    }


# --- expired runs and the janitor -----------------------------------------------

def recover_run(service, old_run_id: str, *, real_now: Optional[datetime] = None) -> str:
    """A live run for payments whose clock run expired (`demoClocks` has a 24 h TTL).

    The new run reads next business day 09:00 ET, already fast-forwarded, and the payments
    are re-tagged to it with `demo.recoveredFrom`. A run that still exists is returned as
    is — there is nothing to recover.
    """
    db = service.db
    if db[business_clock.CLOCKS].find_one({"_id": old_run_id}) is not None:
        return old_run_id
    real_now = real_now or datetime.now(timezone.utc)
    target = cutoff_policy.next_business_day(cutoff_seed.business_date_for(real_now))
    offset = business_clock.anchor_offset(
        cutoff_scenarios._parse_hhmm(NEXT_DAY_OPEN), real_now=real_now, on_date=target)
    run_id = business_clock.create_run(
        db[business_clock.CLOCKS], offset_seconds=offset, real_now=real_now,
        scenario="RECOVERY", business_date=target,
    )
    db[business_clock.CLOCKS].update_one(
        {"_id": run_id}, {"$set": {cutoff_scenarios.FAST_FORWARDED_TO: target.isoformat()}})
    for payment in _tagged_payments(service, old_run_id):
        service.payments.update_one({"_id": payment["_id"]}, {"$set": {
            "demo.clockRunId": run_id, "demo.recoveredFrom": old_run_id}})
    return run_id


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def sweep_stale(service, *, older_than_seconds: float, limit: int = 25,
                real_now: Optional[datetime] = None) -> list[dict]:
    """The janitor's pass: release abandoned runs. Returns one `release_run` result per run.

    A run is abandoned when it is older than `older_than_seconds` and still has an open
    `CUTOFF-DEMO-` payment. Runs the TTL already deleted are recovered first. Driven from
    the payments, not the clocks, because an expired run leaves only its payments behind.
    """
    real_now = real_now or datetime.now(timezone.utc)
    threshold = real_now - timedelta(seconds=older_than_seconds)
    clocks = service.db[business_clock.CLOCKS]
    open_runs: list[str] = []
    for payment in service.payments.find({"demo.clockRunId": {"$exists": True},
                                          "status": {"$in": list(OPEN_STATUSES)}}):
        run_id = payment["demo"]["clockRunId"]
        is_demo = str(payment.get("clientReference") or "").startswith(
            cutoff_scenarios.CLIENT_REF_PREFIX)
        if is_demo and run_id not in open_runs:
            open_runs.append(run_id)

    results = []
    for run_id in open_runs[:limit]:
        run = clocks.find_one({"_id": run_id})
        if run is not None and _aware(run["createdAt"]) > threshold:
            continue
        if run is None:
            run_id = recover_run(service, run_id, real_now=real_now)
        try:
            results.append(release_run(service, run_id, actor=JANITOR_ACTOR, real_now=real_now))
        except ReleaseBusy:
            logger.info("cutoff release janitor: %s is claimed elsewhere", run_id)
    return results
