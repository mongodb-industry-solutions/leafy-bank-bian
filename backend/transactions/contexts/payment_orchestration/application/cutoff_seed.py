"""The cutoff demo's seeded world (cutoff plan Part B): staff, two approvers, a funds-short
account, Lena's backlog and ~14 days of stage-duration history. Pure builders, no I/O —
`backend/data/load_cutoff_seed.py` writes them.

Every document carries `sourceSystem: cutoff-demo-seed`, so the loader can tell what it owns
on a database shared with other demos.

## Off the payment chain

History is `paymentStageEvents` points with `source: "SEED"` and synthetic `HIST-` ids; it
never goes into `payments` (defect 2026-08-31). Lena's backlog is `synthetic: true` approval
requests with no matching payment.

## The story the seed tells (business time, ET)

- Maya, ABC's primary approver, is out of office until 09:00 on the next business day.
- Raj, her backup, works 09:00-19:00, so he can approve C1 at 18:16.
- Lena works 08:00-18:30 and already has six requests open.
- Analysts 1 and 2 leave at 17:00; analyst 3 works 12:00-21:00, so after 17:00 one
  analyst carries the screening queue (C2).
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta, timezone

from contexts.payment_orchestration.domain.cutoff_policy import next_business_day
from shared.business_clock import ET

SOURCE_SYSTEM = "cutoff-demo-seed"
SEED_SOURCE = "SEED"
HISTORY_PREFIX = "HIST-"

ABC_ACCOUNT = "ACC-acc10001"
FUNDS_SHORT_ACCOUNT = "ACC-acc10003"
ABC_OWNER = "CUST-abc10001"
# Signatory ids only: Approve checks account signatories, never the customers collection,
# so no customer documents are seeded. Names live in staffDirectory.
MAYA = "CUST-abc10003"
RAJ = "CUST-abc10004"

STAFF_MAYA = "STAFF-maya"
STAFF_RAJ = "STAFF-raj"
STAFF_LENA = "STAFF-lena"

FUNDS_SHORT_AVAILABLE = 6_300.0
EXPECTED_CREDIT = {"amount": 4_000.0, "currency": "USD", "expectedAtEt": "17:45",
                   "from": "Contoso Retail", "reference": "INV-88213", "status": "EXPECTED"}

# Added to ACC-acc10001's mandate. Fixed `addedAt`, so a re-run never differs.
ABC_SIGNATORIES = [
    {"customerId": MAYA, "type": "JOINT", "signingRule": "JOINT", "addedAt": "2026-10-06"},
    {"customerId": RAJ, "type": "JOINT", "signingRule": "JOINT", "addedAt": "2026-10-06"},
]

DAY_START_ET = 8 * 60
FEDWIRE_CLOSE_ET = 18 * 60 + 45


def _et(day: date, hhmm: str) -> datetime:
    hh, mm = hhmm.split(":")
    return datetime.combine(day, time(int(hh), int(mm)), tzinfo=ET).astimezone(timezone.utc)


def business_date_for(real_now: datetime) -> date:
    """The demo's business date: today in ET, rolled back to Friday at the weekend (Q6), so
    a weekend rehearsal still shows a weekday with its cut-offs and staff on shift."""
    day = real_now.astimezone(ET).date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def maya_back_at(business_date: date) -> datetime:
    """Maya's return: 09:00 ET on the next business day, as UTC."""
    return _et(next_business_day(business_date), "09:00")


def staff_docs(business_date: date) -> list[dict]:
    def person(staff_id, name, role, *, shift=None, customer_id=None, ooo=None,
               backup_for=None, primary_for=(), capacity=None):
        return {
            "staffId": staff_id, "name": name, "role": role, "customerId": customer_id,
            "shift": shift, "outOfOfficeUntil": ooo, "backupFor": backup_for,
            "primaryFor": list(primary_for), "capacityPerHour": capacity,
            "sourceSystem": SOURCE_SYSTEM,
        }

    return [
        person(STAFF_MAYA, "Maya Chen", "APPROVER", customer_id=MAYA,
               ooo=maya_back_at(business_date), primary_for=[ABC_ACCOUNT]),
        person(STAFF_RAJ, "Raj Patel", "APPROVER", customer_id=RAJ, backup_for=STAFF_MAYA,
               shift={"startEt": "09:00", "endEt": "19:00"}),
        person(STAFF_LENA, "Lena Novak", "APPROVER",
               shift={"startEt": "08:00", "endEt": "18:30"}),
        person("STAFF-analyst-1", "Screening Analyst 1", "ANALYST",
               shift={"startEt": "08:00", "endEt": "17:00"}, capacity=6),
        person("STAFF-analyst-2", "Screening Analyst 2", "ANALYST",
               shift={"startEt": "08:00", "endEt": "17:00"}, capacity=6),
        person("STAFF-analyst-3", "Screening Analyst 3", "ANALYST",
               shift={"startEt": "12:00", "endEt": "21:00"}, capacity=6),
    ]


def funds_short_account_doc() -> dict:
    """ABC's payroll account, $3,200 short of C3's wire until Contoso's credit lands."""
    balance = FUNDS_SHORT_AVAILABLE
    return {
        "accountBank": "Leafy Bank",
        "accountId": FUNDS_SHORT_ACCOUNT,
        "accountIdentificationType": "AccountNumber",
        "accountNumber": "310028843",
        "balance": {"current": balance, "available": balance, "ledger": balance, "hold": 0,
                    "overdraftLimit": 0,
                    "updatedAt": datetime(2026, 10, 6, tzinfo=timezone.utc)},
        "branchId": "BRANCH-DEFAULT-001",
        "closedAt": None,
        "createdAt": "2026-10-06T09:00:00.000Z",
        "createdBy": SOURCE_SYSTEM,
        "currency": "USD",
        "customerSnapshot": {"customerId": ABC_OWNER},
        "description": "Payroll account for ABC Manufacturing Inc.",
        "gl": {"accountCode": "2111", "costCenter": "CC-RETAIL-DEFAULT",
               "profitCenter": "PC-RETAIL-DEFAULT"},
        "linkedCardIds": [],
        "linkedMandateIds": [],
        "maturesAt": None,
        "openedAt": "2026-10-06",
        "productId": "PROD-COMM-CA-USD",
        "restrictions": [],
        "signatories": [{"customerId": ABC_OWNER, "type": "PRIMARY", "signingRule": "SOLE",
                         "addedAt": "2026-10-06"}],
        "expectedCredits": [dict(EXPECTED_CREDIT)],
        "sourceSystem": SOURCE_SYSTEM,
        "status": "ACTIVE",
        "type": "CURRENT",
        "version": 1,
    }


def synthetic_lena_requests(business_date: date, n: int = 6) -> list[dict]:
    """Lena's backlog: OPEN approval requests for other clients, with no payment behind them."""
    docs = []
    for i in range(1, n + 1):
        docs.append({
            "approvalRequestId": f"APR-SEED-LENA-{i}",
            "paymentId": f"SYN-LENA-{i}",
            "accountId": f"ACC-SYN-LENA-{i}",
            "amount": 12_000.0 + 1_500.0 * i,
            "currency": "USD",
            "requiredApprovers": [],
            "primaryApprover": STAFF_LENA,
            "assignedTo": STAFF_LENA,
            "requestedAt": _et(business_date, f"{13 + i // 2:02d}:{(i % 2) * 30:02d}"),
            "reminders": [],
            "escalatedTo": None,
            "status": "OPEN",
            "resolvedBy": None,
            "resolvedAt": None,
            "synthetic": True,
            "demo": {"clockRunId": None},
            "sourceSystem": SOURCE_SYSTEM,
        })
    return docs


# --- history ------------------------------------------------------------------

JOURNEYS_PER_DAY = 36
_SEGMENTS = (("COMMERCIAL", 50), ("SME", 30), ("RETAIL", 20))
_FAST_SECONDS = 2.0                  # median of an automated stage

# (median minutes, sigma) for the slow stages. Screening p90 = 5 * e^(1.2816 * 0.45) ≈ 9 min.
_APPROVAL = (25.0, 0.8)
_SCREENING = (5.0, 0.45)
_SCREENING_LATE_FACTOR = 1.8         # after 17:00 one analyst carries the queue
_ROUTED = (3.0, 0.6)
_AUTHORISED = (4.0, 0.6)
_LATE_EXECUTION_FACTOR = 1.5         # after 18:00 the Fedwire rush; C5's remaining-work p90


def _weighted(rng: random.Random, pairs) -> str:
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def _lognormal_seconds(rng: random.Random, median_minutes: float, sigma: float) -> float:
    return median_minutes * 60 * math.exp(rng.gauss(0, sigma))


def _journey(rng: random.Random, start: datetime) -> list[tuple[str, float]]:
    """The stages one payment passes through, as (stage left, seconds spent in it)."""
    rail = "WIRE" if rng.random() < 0.8 else "INTERNAL"
    wire_type = (("DOMESTIC" if rng.random() < 0.6 else "INTERNATIONAL")
                 if rail == "WIRE" else None)
    segment = _weighted(rng, _SEGMENTS)
    amount = 8_000 * math.exp(rng.gauss(0, 1.0))

    def fast():
        return _FAST_SECONDS * math.exp(rng.gauss(0, 0.5))

    states = ["DRAFT", "INITIATED"]
    if segment == "COMMERCIAL" and amount > 10_000 and rng.random() < 0.3:
        states.append("PENDING_APPROVAL")
    states += ["VALIDATED", "ENRICHED", "FINAL_VALIDATED", "ROUTED"]
    if rail == "WIRE" and rng.random() < 0.15:
        states.append("PENDING_SCREENING")
    states += ["AUTHORISED", "APPROVED", "SUBMITTED"]

    steps, at = [], start
    for left in states[:-1]:
        et_minutes = at.astimezone(ET).hour * 60 + at.astimezone(ET).minute
        if left == "PENDING_APPROVAL":
            seconds = _lognormal_seconds(rng, *_APPROVAL)
        elif left == "PENDING_SCREENING":
            seconds = _lognormal_seconds(rng, *_SCREENING)
            if et_minutes >= 17 * 60:
                seconds *= _SCREENING_LATE_FACTOR
        elif left in ("ROUTED", "AUTHORISED"):
            seconds = _lognormal_seconds(rng, *(_ROUTED if left == "ROUTED" else _AUTHORISED))
            if et_minutes >= 18 * 60:
                seconds *= _LATE_EXECUTION_FACTOR
        else:
            seconds = fast()
        at = at + timedelta(seconds=seconds)
        steps.append((left, seconds))
    return {"rail": rail, "wireType": wire_type, "segment": segment}, states, steps


def build_history(*, end_date: date, seed: int = 20261006, days: int = 14) -> list[dict]:
    """Stage-duration points for the `days` calendar days before `end_date` (weekdays only),
    plus `end_date` itself up to Fedwire's 18:45 ET close. Deterministic in its inputs."""
    rng = random.Random(f"{seed}:{end_date.isoformat()}:{days}")
    batch = end_date.isoformat()
    points = []
    for offset in range(days, -1, -1):
        day = end_date - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        close = _et(day, "18:45") if day == end_date else None
        for n in range(JOURNEYS_PER_DAY):
            start_minute = rng.uniform(DAY_START_ET, FEDWIRE_CLOSE_ET - 15)
            start = (datetime.combine(day, time(0), tzinfo=ET)
                     + timedelta(minutes=start_minute)).astimezone(timezone.utc)
            meta, states, steps = _journey(rng, start)
            payment_id = f"{HISTORY_PREFIX}{day:%Y%m%d}-{n:03d}"
            at = start
            for (left, seconds), to_state in zip(steps, states[1:]):
                at = at + timedelta(seconds=seconds)
                if close is not None and at > close:
                    break
                points.append({
                    "at": at,
                    "meta": {"rail": meta["rail"], "segment": meta["segment"], "stage": left},
                    "toState": to_state,
                    "durationMs": int(seconds * 1000),
                    "paymentId": payment_id,
                    "wireType": meta["wireType"],
                    "clockRunId": None,
                    "source": SEED_SOURCE,
                    "seedBatch": batch,
                })
    return points
