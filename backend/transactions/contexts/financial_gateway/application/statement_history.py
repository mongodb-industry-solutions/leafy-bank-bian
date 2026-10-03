"""Backfilled correspondent-statement history: evidence for the reconciliation agent (plan B2).

The agent cites three kinds of fact: a correspondent's charge habit ("BARCGB22 took $25 on
its last four SHAR wires"), its statement lag (p50/p90 of booking minus settlement), and
how past breaks were resolved. Live statements only began on 2026-10-01. This module
fabricates ~14 days of the same evidence: daily camt.053 statements on the USD nostro, plus
the already-resolved exceptions their non-clean lines would have raised.

## Off the live chain (plan B decision D1a)

Every statement carries `statement.historical: true`, and every exception a `historical{}`
subdoc. Live generation (`statement._live_statements`) and the ledger's matcher/orphan
raiser both filter the flag out, so history never moves a live window, never claims a
position and never opens queue work. The wires are synthetic `HIST-` ids with no `payments`
doc: a real wire would also sit on a live statement and be counted twice by the lag query.
No GL events are written — history is evidence, not books.

Lines come from `camt053.entry_for` / `orphan_entry`, the live builders, so a historical
line is shaped exactly like a live one.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from contexts.financial_gateway.domain import camt053
from contexts.financial_gateway.domain.inbound_documents import statement_doc
from process.exceptions import (
    ACTION_ACCEPT_DISCREPANCY,
    ACTION_DISMISS,
    ACTION_ESCALATE_TO_CORRESPONDENT,
    ACTION_LINK_STATEMENT_ENTRY,
    ACTION_POST_ADJUSTMENT,
    ACTION_RECHECK,
    CATEGORY_ORPHANED_SETTLEMENT,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_RECONCILIATION_MISSING,
    SERVICE_LEDGER,
    SEVERITY_ACTION_REQUIRED,
    SOURCE_STAGE_RECONCILE,
    STATUS_RESOLVED,
)
from shared.refs import derive_ref

NOSTRO_USD = "1111"
HISTORY_PREFIX = "HIST-"
ORPHAN = "ORPHAN"

WIRES_PER_DAY = 8
# Plan B mix. Clean dominates, so a statement reads like a normal one.
OUTCOME_WEIGHTS = {
    camt053.CLEAN: 85,
    camt053.FEE_DEDUCTED: 5,
    camt053.REFERENCE_ALTERED: 4,
    camt053.LATE: 3,
    camt053.AMOUNT_TRANSPOSED: 3,
}
# GB carries most volume, which is also the corridor the R1/R1b scenario wires use.
CORRESPONDENT_WEIGHTS = {"BARCGB22": 40, "DEUTDEFF": 25, "UBSWCHZH": 20, "ROYCCAT2": 15}
CHARGE_BEARER_WEIGHTS = {"SHAR": 70, "DEBT": 20, "CRED": 10}
# The R1 beat needs at least this many fee precedents on the GB correspondent, one on DEBT.
# A 5% mix over ~45 GB wires gives ~2 by chance, so the floor is enforced, not hoped for.
FEE_FLOOR_BIC = "BARCGB22"
FEE_FLOOR = 4
ORPHAN_EVERY_N_DAYS = 3

_SYSTEM = "reconciliation-engine"
_OPERATOR = "payments-operations"


def _pick(rng: random.Random, weights: dict) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _amount(rng: random.Random, outcome: str) -> float:
    # A transposition of equal digits is invisible (it degrades to CLEAN), so re-draw.
    while True:
        amount = round(rng.uniform(800, 45_000), 2)
        if outcome != camt053.AMOUNT_TRANSPOSED or camt053.transposed_amount(amount) != amount:
            return amount


def plan_wires(start: datetime, days: int, rng: random.Random) -> list[dict]:
    """The synthetic wires, one dict each: id, day, settledAt, amount, bic, chargeBearer, outcome."""
    wires = []
    for day in range(days):
        day_start = start + timedelta(days=day)
        for _ in range(WIRES_PER_DAY):
            wires.append({
                "paymentId": f"{HISTORY_PREFIX}{rng.randrange(16**8):08x}",
                "day": day,
                # Working hours, so the lag to the next-midnight booking is realistic.
                "settledAt": day_start + timedelta(minutes=rng.randrange(8 * 60, 18 * 60)),
                "bic": _pick(rng, CORRESPONDENT_WEIGHTS),
                "chargeBearer": _pick(rng, CHARGE_BEARER_WEIGHTS),
                "outcome": _pick(rng, OUTCOME_WEIGHTS),
            })
    # LATE on the last day would land on a statement that does not exist yet.
    for w in wires:
        if w["outcome"] == camt053.LATE and w["day"] == days - 1:
            w["outcome"] = camt053.CLEAN
    _enforce_fee_floor(wires)
    for w in wires:
        w["amount"] = _amount(rng, w["outcome"])
    return wires


def _enforce_fee_floor(wires: list[dict]) -> None:
    fees = [w for w in wires if w["bic"] == FEE_FLOOR_BIC and w["outcome"] == camt053.FEE_DEDUCTED]
    spare = [w for w in wires if w["bic"] == FEE_FLOOR_BIC and w["outcome"] == camt053.CLEAN]
    while len(fees) < FEE_FLOOR and spare:
        w = spare.pop()
        w["outcome"] = camt053.FEE_DEDUCTED
        fees.append(w)
    if fees and not any(w["chargeBearer"] == "DEBT" for w in fees):
        fees[0]["chargeBearer"] = "DEBT"


def _resolution_for(outcome: str, charge_bearer: str) -> tuple[str, str]:
    """(category, action) for one non-clean line, matching what A3/A4 would have done."""
    if outcome == camt053.FEE_DEDUCTED:
        # Decision 2: chargeBearer decides the books.
        return CATEGORY_RECONCILIATION_DISCREPANCY, (
            ACTION_POST_ADJUSTMENT if charge_bearer == "DEBT" else ACTION_ACCEPT_DISCREPANCY)
    if outcome == camt053.REFERENCE_ALTERED:
        return CATEGORY_RECONCILIATION_MISSING, ACTION_LINK_STATEMENT_ENTRY
    if outcome == camt053.LATE:
        return CATEGORY_RECONCILIATION_MISSING, ACTION_RECHECK
    if outcome == camt053.AMOUNT_TRANSPOSED:
        # Escalated, then the correspondent's corrected line cleared it on re-check.
        return CATEGORY_RECONCILIATION_DISCREPANCY, ACTION_RECHECK
    if outcome == ORPHAN:
        return CATEGORY_ORPHANED_SETTLEMENT, ACTION_DISMISS
    raise ValueError(f"no resolution for outcome {outcome!r}")


_NOTES = {
    (camt053.FEE_DEDUCTED, ACTION_POST_ADJUSTMENT): "Correspondent deducted its charge on an OUR (DEBT) wire; booked to 5214.",
    (camt053.FEE_DEDUCTED, ACTION_ACCEPT_DISCREPANCY): "Correspondent charge on a shared/beneficiary-borne wire; accepted, no entry.",
    (camt053.REFERENCE_ALTERED, ACTION_LINK_STATEMENT_ENTRY): "Correspondent re-keyed the reference; same amount and value date — linked.",
    (camt053.LATE, ACTION_RECHECK): "Line booked one statement late; cleared on re-check.",
    (camt053.AMOUNT_TRANSPOSED, ACTION_RECHECK): "Digits transposed on the statement; investigation request sent, corrected line received.",
    (ORPHAN, ACTION_DISMISS): "Statement line with no internal payment; correspondent confirmed a misposting.",
}


def _exception(*, payment_id, category, action, outcome, bic, charge_bearer, stmt, line_no,
               raised_at, detail, subject_ref=None, escalated=False) -> dict:
    oid = ObjectId()
    resolved_at = raised_at + timedelta(hours=2 if action != ACTION_RECHECK else 0.5)
    by = _SYSTEM if (action == ACTION_RECHECK and outcome == camt053.LATE) else _OPERATOR
    return {
        "_id": oid,
        "exceptionId": derive_ref("EXC", oid),
        "paymentId": payment_id,
        "category": category,
        "status": STATUS_RESOLVED,
        "severity": SEVERITY_ACTION_REQUIRED,
        "source": {"stage": SOURCE_STAGE_RECONCILE, "service": SERVICE_LEDGER},
        "detail": detail,
        "subjectRef": subject_ref,
        "resolution": {"action": action, "by": by, "at": resolved_at,
                       "note": _NOTES[(outcome, action)]},
        "awaitingCounterparty": False,
        # No camt.026 doc exists for history, so no `paymentMessageId` either.
        "escalation": ({"by": _OPERATOR, "at": raised_at + timedelta(minutes=20), "note": None}
                       if escalated else None),
        "agent": None,
        "historical": {
            "correspondentBic": bic,
            "chargeBearer": charge_bearer,
            "statementOutcome": outcome,
            "paymentMessageId": stmt["paymentMessageId"],
            "lineNo": line_no,
        },
        "createdAt": raised_at,
        "updatedAt": resolved_at,
        "sourceSystem": SERVICE_LEDGER,
    }


def build_history(*, now: datetime, days: int = 14, seed: int = 1039,
                  account_code: str = NOSTRO_USD) -> tuple[list[dict], list[dict]]:
    """(statements, exceptions) for the `days` full UTC days before `now`. Pure, deterministic."""
    rng = random.Random(seed)
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days)
    wires = plan_wires(start, days, rng)

    statements, exceptions = [], []
    opening = 0.0
    for day in range(days):
        window_from = start + timedelta(days=day)
        window_to = window_from + timedelta(days=1)
        # Today's own wires, minus LATE ones (held), plus yesterday's LATE ones.
        due = [w for w in wires
               if (w["day"] == day and w["outcome"] != camt053.LATE)
               or (w["day"] == day - 1 and w["outcome"] == camt053.LATE)]
        due.sort(key=lambda w: w["settledAt"])
        entries = []
        for w in due:
            position = {"paymentId": w["paymentId"], "grossAmount": w["amount"], "currency": "USD"}
            outcome = None if w["outcome"] in (camt053.CLEAN, camt053.LATE) else w["outcome"]
            entry = camt053.entry_for(position, outcome)
            entry["simulatedOutcome"] = w["outcome"]
            entry["counterpartyBic"] = w["bic"]
            entry["settledAt"] = w["settledAt"]    # lag evidence: bookingDate − settledAt
            entries.append(entry)
        if day % ORPHAN_EVERY_N_DAYS == 1:
            entries.append(camt053.orphan_entry(rng, sorted(CORRESPONDENT_WEIGHTS)))

        oid = ObjectId()
        message = camt053.build(
            statement_id=derive_ref("STMT", oid), account_code=account_code, currency="USD",
            window_from=window_from, window_to=window_to, sequence=day + 1,
            opening_balance=opening, entries=entries, now=window_to)
        doc = statement_doc(
            oid=oid, message=message, account_code=account_code, currency="USD",
            window_from=window_from, window_to=window_to, sequence=day + 1,
            opening_balance=opening, closing_balance=camt053.closing_balance(opening, entries),
            entries=entries, now=window_to)
        doc["statement"]["historical"] = True
        opening = doc["statement"]["closingBalance"]

        by_pid = {w["paymentId"]: w for w in due}
        for line in doc["entries"]:
            line_no = line["lineNo"]
            wire = by_pid.get(line.get("simulatedPaymentId"))
            outcome = wire["outcome"] if wire else ORPHAN
            if wire is not None:
                line["recon"] = {"status": "AUTO_MATCHED" if outcome in (camt053.CLEAN, camt053.LATE)
                                 else "MANUAL_MATCHED",
                                 "matchedPaymentId": wire["paymentId"],
                                 "matchedBy": "EXACT_KEY" if outcome in (camt053.CLEAN, camt053.LATE)
                                 else _OPERATOR,
                                 "at": window_to}
            if outcome == camt053.CLEAN:
                continue
            charge_bearer = wire["chargeBearer"] if wire else "SHAR"
            category, action = _resolution_for(outcome, charge_bearer)
            # LATE is noticed when its own day's statement lacks it; everything else on booking.
            raised_at = (window_from if outcome == camt053.LATE else window_to) + timedelta(minutes=5)
            common = dict(outcome=outcome, bic=line["counterpartyBic"], charge_bearer=charge_bearer,
                          stmt=doc, line_no=line_no, raised_at=raised_at)
            if wire is None:
                exc = _exception(payment_id=f"{doc['paymentMessageId']}#{line_no}",
                                 category=category, action=action, escalated=True,
                                 subject_ref={"kind": "STATEMENT_LINE",
                                              "paymentMessageId": doc["paymentMessageId"],
                                              "lineNo": line_no},
                                 detail={"discrepancyAmount": None, "actualAmount": line["amount"],
                                         "reference": line["reference"], "currency": "USD"},
                                 **common)
                line["recon"] = {**line["recon"], "exceptionId": exc["exceptionId"]}
                exceptions.append(exc)
                continue
            delta = round(wire["amount"] - line["amount"], 2)
            exceptions.append(_exception(
                payment_id=wire["paymentId"], category=category, action=action,
                escalated=outcome == camt053.AMOUNT_TRANSPOSED,
                detail={"discrepancyAmount": delta or None, "expectedAmount": wire["amount"],
                        "actualAmount": line["amount"],
                        "discrepancyReason": _NOTES[(outcome, action)]},
                **common))
            if outcome == camt053.REFERENCE_ALTERED:
                # The line's own ORPHANED twin, closed by the same LINK.
                exceptions.append(_exception(
                    payment_id=f"{doc['paymentMessageId']}#{line_no}",
                    category=CATEGORY_ORPHANED_SETTLEMENT, action=ACTION_LINK_STATEMENT_ENTRY,
                    subject_ref={"kind": "STATEMENT_LINE",
                                 "paymentMessageId": doc["paymentMessageId"], "lineNo": line_no},
                    detail={"discrepancyAmount": None, "actualAmount": line["amount"],
                            "reference": line["reference"], "currency": "USD"},
                    **common))
        statements.append(doc)
    return statements, exceptions
