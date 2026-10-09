"""The cutoff demo scenarios C1-C5 (cutoff plan Part B), and the presenter's clock.

Each scenario anchors a fresh demo clock run to a business minute, sets up what the story
needs, then starts the wire through the real `initiate_payment(clock_run_id=...)` — the
reachability lesson (defect 2026-09-01): a hold is only demonstrable if the running system
can produce it.

| Key | Anchor ET | Debtor        | Wire                    | Expected hold                        |
|-----|-----------|---------------|-------------------------|--------------------------------------|
| C1  | 17:05     | ACC-acc10001  | 25,000 DOMESTIC         | PENDING_APPROVAL, Maya OOO           |
| C2  | 17:50     | ACC-acc10001  | 8,750 INTERNATIONAL AE  | PENDING_SCREENING, position 8        |
| C3  | 16:45     | ACC-acc10003  | available + 3,200 DOM.  | PENDING_FUNDS, short 3,200           |
| C4  | 16:30 +4m | ACC-acc10001  | 6,400 INTERNATIONAL GB  | PENDING_SCREENING, position 2        |
| C5  | 18:35     | ACC-acc10001  | 7,900 INTERNATIONAL CA  | CUTOFF_EXCEPTION, 10 min to Fedwire  |

Every wire states its `wireType`, so it meets a Fedwire 18:45 external cut-off (G1).
Domestic wires go `HIGH` priority, which routes them to Fedwire.

The two screening scenarios use the potential-match names from `sanctions` by reference,
never as literals here: no simulator source may carry them (pinned by a test), and that
includes this one.

Re-running a scenario first supersedes its earlier holds (Q3): the payment is rejected and
its open records closed as CANCELLED, so one story never shows two copies of itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from contexts.fraud_evaluation.domain import sanctions
from contexts.payment_order_initiation.application import funds_reservation
from contexts.payment_order_initiation.domain import lifecycle
from contexts.payment_orchestration.application import cutoff_seed
from contexts.payment_orchestration.domain import cutoff_policy
from shared import business_clock, hold_queues

CLIENT_REF_PREFIX = "CUTOFF-DEMO-"
ACTOR = "cutoff-demo"
SUPERSEDED_REASON = "Superseded by a new demo run."
C3_SHORT_BY = 3_200.0
# Set on a run once `move_clock(next_business_day=...)` has moved it; the day it reached.
FAST_FORWARDED_TO = "fastForwardedTo"

HOLDS = (lifecycle.PENDING_APPROVAL, lifecycle.PENDING_FUNDS,
         lifecycle.PENDING_SCREENING, lifecycle.CUTOFF_EXCEPTION)


def _potential(prefix: str) -> str:
    return next(n for n in sorted(sanctions.potential_matches()) if n.startswith(prefix))


_CREDITORS = {
    "C1": {"name": "Midwest Steel Supply Co", "accountNo": "4410028871", "bic": "CHASUS33",
           "bankName": "JPMorgan Chase", "bankCountry": "US",
           "address": "2200 S Halsted St, Chicago"},
    "C2": {"name": _potential("NORTHGATE"), "accountNo": "AE070331234567890123456",
           "bic": "EBILAEAD", "bankName": "Emirates NBD", "bankCountry": "AE",
           "address": "Jebel Ali Free Zone, Dubai"},
    "C3": {"name": "Lakeside Components LLC", "accountNo": "3310045592", "bic": "BOFAUS3N",
           "bankName": "Bank of America", "bankCountry": "US",
           "address": "88 Harbor Rd, Cleveland"},
    "C4": {"name": _potential("SEVERN"), "accountNo": "GB29NWBK60161331926819",
           "bic": "BARCGB22", "bankName": "Barclays Bank PLC", "bankCountry": "GB",
           "address": "4 Dock Street, Bristol", "clearingSystemCode": "GBDSC",
           "clearingSystemMemberId": "202053"},
    "C5": {"name": "Tailwind Freight Inc", "accountNo": "003000012345678", "bic": "ROYCCAT2",
           "bankName": "Royal Bank of Canada", "bankCountry": "CA",
           "address": "77 Harbour Street, Toronto", "clearingSystemCode": "CACPA",
           "clearingSystemMemberId": "000300002"},
}


@dataclass(frozen=True)
class Scenario:
    key: str
    anchor_et_minutes: int
    debtor_account: str
    wire_type: str
    amount: Optional[float]                 # None: computed at run time (C3)
    expected_status: str
    purpose: str
    synthetic_ahead_minutes: tuple = field(default_factory=tuple)
    advance_minutes: int = 0

    @property
    def priority(self) -> str:
        return "HIGH" if self.wire_type == "DOMESTIC" else "NORMAL"


SCENARIOS: dict[str, Scenario] = {s.key: s for s in (
    Scenario("C1", 17 * 60 + 5, cutoff_seed.ABC_ACCOUNT, "DOMESTIC", 25_000.0,
             lifecycle.PENDING_APPROVAL, "Steel coil order PO-55120"),
    Scenario("C2", 17 * 60 + 50, cutoff_seed.ABC_ACCOUNT, "INTERNATIONAL", 8_750.0,
             lifecycle.PENDING_SCREENING, "Machine parts INV-7781",
             synthetic_ahead_minutes=(35, 30, 25, 20, 15, 10, 5)),
    Scenario("C3", 16 * 60 + 45, cutoff_seed.FUNDS_SHORT_ACCOUNT, "DOMESTIC", None,
             lifecycle.PENDING_FUNDS, "Component supply INV-3391"),
    Scenario("C4", 16 * 60 + 30, cutoff_seed.ABC_ACCOUNT, "INTERNATIONAL", 6_400.0,
             lifecycle.PENDING_SCREENING, "Charter freight INV-2207",
             synthetic_ahead_minutes=(10,), advance_minutes=4),
    Scenario("C5", 18 * 60 + 35, cutoff_seed.ABC_ACCOUNT, "INTERNATIONAL", 7_900.0,
             lifecycle.CUTOFF_EXCEPTION, "Cross-border freight INV-6604"),
)}


def _as_float(value) -> float:
    return float(str(value)) if value is not None else 0.0


def _clocks(db):
    return db[business_clock.CLOCKS]


def _clock_view(run_id: str, offset_seconds: int, *, real_now: Optional[datetime] = None) -> dict:
    business = (real_now or datetime.now(timezone.utc)) + timedelta(seconds=offset_seconds)
    return {
        "runId": run_id,
        "offsetSeconds": offset_seconds,
        "businessNow": business,
        "businessNowEt": business_clock.to_et(business).isoformat(timespec="seconds"),
    }


def _supersede(service, key: str, *, at: datetime) -> list[str]:
    """Reject this scenario's earlier holds and cancel their open records (Q3)."""
    db = service.db
    superseded = []
    for payment in service.payments.find({"clientReference": f"{CLIENT_REF_PREFIX}{key}",
                                          "status": {"$in": list(HOLDS)}}):
        payment_id = payment["paymentId"]
        lifecycle.reject(service.payments, payment["_id"], reason=SUPERSEDED_REASON, actor=ACTOR)
        funds_reservation.release_for_payment(db, payment_id, reason=SUPERSEDED_REASON)
        hold_queues.close_approval_request(db, payment_id=payment_id,
                                           status=hold_queues.CANCELLED, by=ACTOR, at=at)
        hold_queues.close_screening_item(db, payment_id=payment_id,
                                         status=hold_queues.CANCELLED, by=ACTOR, at=at)
        run_id = (payment.get("demo") or {}).get("clockRunId")
        if run_id:
            # The old run's synthetic backlog dies with it; the TTL would get there in a day.
            db[hold_queues.SCREENING_QUEUE].update_many(
                {"demo.clockRunId": run_id, "status": hold_queues.OPEN, "synthetic": True},
                {"$set": {"status": hold_queues.CANCELLED, "resolvedBy": ACTOR,
                          "resolvedAt": at}},
            )
        superseded.append(payment_id)
    return superseded


def _enqueue_backlog(db, run_id: str, scenario: Scenario, now: datetime) -> None:
    for i, minutes in enumerate(scenario.synthetic_ahead_minutes, start=1):
        queued_at = now - timedelta(minutes=minutes)
        hold_queues.enqueue_screening(
            db, payment={"paymentId": f"SYN-{run_id}-{i}", "demo": {"clockRunId": run_id}},
            reason="Synthetic backlog item (cutoff demo).", matched=None, at=queued_at,
            synthetic=True, queued_at=queued_at,
        )


def _c3_amount(service) -> float:
    account = service.accounts.find_one({"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT}) or {}
    if not account:
        raise ValueError(f"{cutoff_seed.FUNDS_SHORT_ACCOUNT} is not seeded; run "
                         "load_cutoff_seed.py.")
    if account.get("sourceSystem") != cutoff_seed.SOURCE_SYSTEM:
        raise ValueError(f"{cutoff_seed.FUNDS_SHORT_ACCOUNT} is not owned by the cutoff demo "
                         "seed; refusing to size C3 from it.")
    available = _as_float((account.get("balance") or {}).get("available"))
    if available != cutoff_seed.FUNDS_SHORT_AVAILABLE:
        # Earlier runs' funds credits and settlements leave the seed account off its
        # opening balance. Restore it so every C3 run starts from the same story.
        _reset_funds_short_balance(service)
        available = cutoff_seed.FUNDS_SHORT_AVAILABLE
    return round(available + C3_SHORT_BY, 2)


def _reset_funds_short_balance(service) -> None:
    now = datetime.now(timezone.utc)
    opening = cutoff_seed.FUNDS_SHORT_AVAILABLE
    service.accounts.update_one(
        {"accountId": cutoff_seed.FUNDS_SHORT_ACCOUNT,
         "sourceSystem": cutoff_seed.SOURCE_SYSTEM},
        {"$set": {"balance.current": opening, "balance.available": opening,
                  "balance.ledger": opening, "balance.updatedAt": now, "updatedAt": now}},
    )


def _details(service, scenario: Scenario, payment: dict, now: datetime) -> dict:
    db = service.db
    payment_id = payment["paymentId"]
    out: dict = {}
    if scenario.key == "C1":
        request = db[hold_queues.APPROVAL_REQUESTS].find_one(
            {"paymentId": payment_id, "status": hold_queues.OPEN}) or {}
        out["approvalRequest"] = {k: request.get(k) for k in (
            "approvalRequestId", "primaryApprover", "assignedTo", "requiredApprovers")}
    if scenario.synthetic_ahead_minutes:
        out["screening"] = hold_queues.screening_position(db, payment_id=payment_id, at=now)
        events = (payment.get("lifecycle") or {}).get("events") or []
        if events:
            entered = events[-1]["at"]
            entered = entered if entered.tzinfo else entered.replace(tzinfo=timezone.utc)
            out["minutesInStage"] = round((now - entered).total_seconds() / 60, 1)
    if scenario.key == "C3":
        account = service.accounts.find_one({"accountId": scenario.debtor_account}) or {}
        available = _as_float((account.get("balance") or {}).get("available"))
        out["shortBy"] = round(payment["instructedAmount"] - available, 2)
    window = cutoff_policy.window_for(rail=payment.get("rail"), wire_type=scenario.wire_type,
                                      currency=payment.get("instructedCurrency"))
    if window is not None:
        external = cutoff_policy.cutoff_at(window, business_date=business_clock.to_et(now).date(),
                                           which="external")
        out["externalCutoffAt"] = external
        out["minutesToExternalCutoff"] = round((external - now).total_seconds() / 60, 1)
    return out


def run_one(service, key: str, *, real_now: Optional[datetime] = None) -> dict:
    """Start one scenario on a fresh clock run. Raises KeyError for an unknown key and
    ValueError when the seeded world cannot produce it (C3 over its ceiling)."""
    scenario = SCENARIOS[key]
    real_now = real_now or datetime.now(timezone.utc)
    db = service.db

    superseded = _supersede(service, key, at=real_now)

    business_date = cutoff_seed.business_date_for(real_now)
    offset = business_clock.anchor_offset(scenario.anchor_et_minutes, real_now=real_now,
                                          on_date=business_date)
    run_id = business_clock.create_run(
        _clocks(db), offset_seconds=offset, real_now=real_now, scenario=key,
        anchor_et_minutes=scenario.anchor_et_minutes, business_date=business_date,
    )
    business_now = business_clock.now(run_id, clocks=_clocks(db))

    if key == "C1":
        db[hold_queues.STAFF_DIRECTORY].update_one(
            {"staffId": cutoff_seed.STAFF_MAYA},
            {"$set": {"outOfOfficeUntil": cutoff_seed.maya_back_at(business_date)}},
        )
    amount = scenario.amount if scenario.amount is not None else _c3_amount(service)
    _enqueue_backlog(db, run_id, scenario, business_now)

    payment = service.initiate_payment(
        customer_ref=cutoff_seed.ABC_OWNER,
        debtor_account_ref=scenario.debtor_account,
        creditor_account_ref=None,
        creditor_party=dict(_CREDITORS[key]),
        instructed_amount=amount,
        instructed_currency="USD",
        payment_type="CREDIT_TRANSFER",
        payment_rail="WIRE",
        remittance_unstructured=scenario.purpose,
        priority=scenario.priority,
        channel="BRANCH",
        client_reference=f"{CLIENT_REF_PREFIX}{key}",
        wire_details={"wireType": scenario.wire_type},
        clock_run_id=run_id,
    )

    clock = _clock_view(run_id, offset)
    if scenario.advance_minutes:
        clock = move_clock(db, run_id, advance_minutes=scenario.advance_minutes)
    business_now = business_clock.now(run_id, clocks=_clocks(db))

    result = {
        "scenario": key,
        **clock,
        "paymentId": payment["paymentId"],
        "status": payment["status"],
        "expectedStatus": scenario.expected_status,
        "amount": amount,
        "wireType": scenario.wire_type,
        "superseded": superseded,
        **_details(service, scenario, payment, business_now),
    }
    if payment["status"] != scenario.expected_status:
        result["warning"] = (f"{key} reached {payment['status']}, not "
                             f"{scenario.expected_status}.")
    return result


def _parse_hhmm(value: str) -> int:
    hh, mm = value.split(":")
    minutes = int(hh) * 60 + int(mm)
    if not (0 <= int(mm) < 60 and 0 <= minutes < 24 * 60):
        raise ValueError(f"Invalid anchor {value!r}; expected HH:MM.")
    return minutes


def move_clock(db, run_id: str, *, anchor: Optional[str] = None,
               advance_minutes: Optional[int] = None, reset: bool = False,
               next_business_day: Optional[str] = None,
               real_now: Optional[datetime] = None) -> dict:
    """Move one run's business clock. Exactly one action.

    `anchor` (HH:MM ET) and `advance_minutes` only move forward — a held payment's stage
    events must never go back in time. `reset` returns to the run's start minute (Q7), the
    one deliberate exception. `next_business_day` (HH:MM ET) jumps to that minute on the
    business day after the run's date and moves the run's `businessDate` with it; it is the
    cutoff release's option and is not exposed on `DemoClockRequest`. Calling it again is a
    no-op: `FAST_FORWARDED_TO` remembers the day already reached. Raises LookupError for an
    unknown run, ValueError otherwise.
    """
    actions = (anchor is not None, advance_minutes is not None, bool(reset),
               next_business_day is not None)
    if sum(actions) != 1:
        raise ValueError("Give exactly one of anchor, advanceMinutes or reset.")
    run = _clocks(db).find_one({"_id": run_id})
    if run is None:
        raise LookupError(f"Clock run {run_id} not found.")

    real_now = real_now or datetime.now(timezone.utc)
    current = int(run.get("offsetSeconds") or 0)
    business_now = real_now + timedelta(seconds=current)
    business_date = (date.fromisoformat(run["businessDate"]) if run.get("businessDate")
                     else business_clock.to_et(business_now).date())

    if anchor is not None:
        offset = business_clock.anchor_offset(_parse_hhmm(anchor), real_now=real_now,
                                              on_date=business_date)
        # Compare whole minutes: anchoring to the minute already showing is not "backwards".
        if offset < current - business_now.second - 1:
            raise ValueError(
                f"Anchor {anchor} is before the run's business time "
                f"{business_clock.to_et(business_now):%H:%M}; the clock only moves forward."
            )
        offset = max(offset, current)
    elif advance_minutes is not None:
        if advance_minutes <= 0:
            raise ValueError("advanceMinutes must be positive; the clock only moves forward.")
        offset = current + int(advance_minutes) * 60
    elif next_business_day is not None:
        target_date = (date.fromisoformat(run[FAST_FORWARDED_TO]) if run.get(FAST_FORWARDED_TO)
                       else cutoff_policy.next_business_day(business_date))
        offset = business_clock.anchor_offset(_parse_hhmm(next_business_day), real_now=real_now,
                                              on_date=target_date)
        if offset < current - business_now.second - 1:
            return _clock_view(run_id, current, real_now=real_now)
        offset = max(offset, current)
        _clocks(db).update_one({"_id": run_id}, {"$set": {
            "offsetSeconds": offset, "businessDate": target_date.isoformat(),
            FAST_FORWARDED_TO: target_date.isoformat(),
        }})
        business_clock.forget(run_id)
        return _clock_view(run_id, offset, real_now=real_now)
    else:
        if run.get("anchorEtMinutes") is None:
            raise ValueError(f"Clock run {run_id} has no start minute to reset to.")
        offset = business_clock.anchor_offset(int(run["anchorEtMinutes"]), real_now=real_now,
                                              on_date=business_date)

    _clocks(db).update_one({"_id": run_id}, {"$set": {"offsetSeconds": offset}})
    business_clock.forget(run_id)
    return _clock_view(run_id, offset, real_now=real_now)
