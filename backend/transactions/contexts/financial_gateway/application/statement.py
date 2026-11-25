"""Generate the correspondent's camt.053 statement for one nostro (reconciliation plan A1).

The simulator side of the external bank: it books every outbound wire that **settled** on
the nostro since the previous statement, applies each payment's `simulatedStatementOutcome`,
optionally adds one orphan line, and stores the result in `canonicalJsonStorage`.

## What goes on a statement

A wire is booked when it settles (`clearing.settledAt`, stamped by `settle.complete_due`),
not when its `settlementPositions` doc is written. The position reads SETTLED at capture
while the payment is still PENDING, so keying on it would book wires that have not settled,
and would lose a wire captured in one window but settled in the next.

Windows chain: each statement runs from the previous one's `window.to` to now, so every
settled wire falls in exactly one window. A LATE line is held out of its own window and
booked on the next statement.

## Idempotency

Nothing is written when no settled wire is unbooked. A statement whose only wires are
LATE is still written — with no lines for them — because the next statement is where they
land; skipping it would hold them forever. Two concurrent callers compute the same
`window.from`; `idx_statement_window_unique` rejects the loser, which returns the winner.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from contexts.financial_gateway.domain import camt053
from contexts.financial_gateway.domain.inbound_documents import (
    PURPOSE_ACCOUNT_STATEMENT,
    statement_doc,
)
from contexts.payment_orchestration.domain.routing import _CORRESPONDENT_BY_COUNTRY
from shared.refs import derive_ref

logger = logging.getLogger(__name__)

NOSTRO_USD = "1111"

# Scenario wires (`recon_scenarios`) carry this clientReference prefix. The presenter books
# their statement by hand, so the background simulator must leave them alone — otherwise any
# instance sharing the DB (e.g. a deployed one with the sim on) books them mid-walkthrough.
PRESENTER_DRIVEN_REF_PREFIX = "RECON-DEMO-"
# Cut-off demo wires settle against a seeded account with no statement story. Booking them
# would turn a cut-off walkthrough into a reconciliation exception, so the simulator skips
# them too (the ledger's sweep skips the same prefix).
CUTOFF_DEMO_REF_PREFIX = "CUTOFF-DEMO-"
_SIMULATOR_SKIPPED_PREFIXES = (PRESENTER_DRIVEN_REF_PREFIX, CUTOFF_DEMO_REF_PREFIX)


def _as_datetime(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value))


def _live_statements(account_code: str) -> dict:
    # Plan B: backfilled history (`statement.historical`) is off the live chain — it must
    # neither be the previous window nor count a payment as already booked.
    return {"purpose": PURPOSE_ACCOUNT_STATEMENT, "statement.accountCode": account_code,
            "statement.historical": {"$ne": True}}


def _previous_statement(messages, account_code: str) -> Optional[dict]:
    statements = list(messages.find(
        _live_statements(account_code),
        {"statement": 1, "entries.simulatedPaymentId": 1, "paymentMessageId": 1},
    ))
    if not statements:
        return None
    return max(statements, key=lambda s: s["statement"]["sequence"])


def _settled_wires(payments, account_code: str, *, skip_presenter_driven: bool = False) -> list[dict]:
    """Outbound wires settled on this nostro. The window is applied by the caller."""
    wires = payments.find(
        {
            "lifecycle.settlementStatus": "SETTLED",
            "clearing.settlementAccountCode": account_code,
            "clearing.settledAt": {"$ne": None},
            "direction": {"$ne": "INBOUND"},
        },
        {"paymentId": 1, "clearing.settledAt": 1, "simulatedStatementOutcome": 1,
         "clientReference": 1, "remittance": 1, "creditor.name": 1,
         "correspondent.correspondentBic": 1},
    )
    if not skip_presenter_driven:
        return list(wires)
    return [w for w in wires
            if not (w.get("clientReference") or "").startswith(_SIMULATOR_SKIPPED_PREFIXES)]


def _booked_payment_ids(messages, account_code: str) -> set[str]:
    booked = set()
    for s in messages.find(
        _live_statements(account_code),
        {"entries.simulatedPaymentId": 1},
    ):
        booked.update(e.get("simulatedPaymentId") for e in s.get("entries", []))
    booked.discard(None)
    return booked


def generate_statement(
    db,
    *,
    account_code: str = NOSTRO_USD,
    include_orphan: bool = True,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
    skip_presenter_driven: bool = False,
) -> Optional[dict]:
    """Write the next statement for `account_code`. Returns it, or None when nothing is due."""
    now = now or datetime.now(timezone.utc)
    messages = db["canonicalJsonStorage"]
    previous = _previous_statement(messages, account_code)
    booked = _booked_payment_ids(messages, account_code)

    wires = [w for w in _settled_wires(db["payments"], account_code,
                                       skip_presenter_driven=skip_presenter_driven)
             if w["paymentId"] not in booked]
    for w in wires:
        w["_settledAt"] = _as_datetime(w["clearing"]["settledAt"])
    wires = [w for w in wires if w["_settledAt"] <= now]
    if not wires:
        return None

    window_from = (_as_datetime(previous["statement"]["window"]["to"]) if previous
                   else min(w["_settledAt"] for w in wires))

    # In this window, unless LATE (held one statement). A wire settled before `window_from`
    # and never booked is a LATE line from the previous window: book it now.
    due = [w for w in wires
           if w["_settledAt"] < window_from
           or (w["_settledAt"] >= window_from
               and w.get("simulatedStatementOutcome") != camt053.LATE)]
    positions = {p["paymentId"]: p for p in db["settlementPositions"].find(
        {"paymentId": {"$in": [w["paymentId"] for w in due]}},
    )}
    entries = []
    for w in sorted(due, key=lambda w: w["_settledAt"]):
        position = positions.get(w["paymentId"])
        if position is None:
            logger.warning("statement: %s settled with no settlementPositions doc — skipped",
                           w["paymentId"])
            continue
        outcome = w.get("simulatedStatementOutcome")
        # LATE has done its job by arriving a statement late; the line itself is clean.
        # A CHARGE_CLAIMED charge is levied by the beneficiary's bank, which for these
        # corridors is the country correspondent on the payment.
        entries.append(camt053.entry_for(
            position, None if outcome == camt053.LATE else outcome, camt053.narrative_for(w),
            charging_agent_bic=(w.get("correspondent") or {}).get("correspondentBic")))
    if include_orphan:
        rng = rng or random.Random(f"{account_code}:{window_from.isoformat()}")
        entries.append(camt053.orphan_entry(rng, sorted(_CORRESPONDENT_BY_COUNTRY.values())))

    oid = ObjectId()
    sequence = previous["statement"]["sequence"] + 1 if previous else 1
    opening = previous["statement"]["closingBalance"] if previous else 0.0
    # Both settlement accounts are USD; an all-LATE statement has no line to read it from.
    currency = entries[0]["currency"] if entries else "USD"
    message = camt053.build(
        statement_id=derive_ref("STMT", oid),
        account_code=account_code,
        currency=currency,
        window_from=window_from,
        window_to=now,
        sequence=sequence,
        opening_balance=opening,
        entries=entries,
        now=now,
    )
    doc = statement_doc(
        oid=oid,
        message=message,
        account_code=account_code,
        currency=currency,
        window_from=window_from,
        window_to=now,
        sequence=sequence,
        opening_balance=opening,
        closing_balance=camt053.closing_balance(opening, entries),
        entries=entries,
        now=now,
    )
    try:
        messages.insert_one(doc)
    except DuplicateKeyError:
        # A concurrent caller wrote this window first; theirs is the statement.
        return messages.find_one({
            "purpose": PURPOSE_ACCOUNT_STATEMENT,
            "statement.accountCode": account_code,
            "statement.window.from": window_from,
        })
    logger.info("statement %s (seq %d) for %s: %d lines",
                doc["paymentMessageId"], sequence, account_code, len(entries))
    return doc


# ── Correcting statement (reconciliation plan A4, 2026-11-10) ──────────────────────────
#
# A correspondent's camt.111 reply is a statement of intent, not accounting evidence: it
# cannot move the booked amount. What closes an escalated discrepancy is the correspondent's
# next camt.053 actually booking the expected amount — the reversal of the mis-booked line
# and the rebooking at 7,360.00 for R4. This is that statement. The ledger's statement
# matching applies it; the exception closes only when the tie-out then agrees.

CORRECTED_STATEMENT_SECONDS = 20  # simulated: after the camt.111, not a SWIFT service level


def _original_line(messages, payment_id: str) -> Optional[tuple[dict, dict]]:
    """The live statement line matched to this payment, with its statement."""
    stmt = messages.find_one(
        {"purpose": PURPOSE_ACCOUNT_STATEMENT, "statement.historical": {"$ne": True},
         "entries": {"$elemMatch": {"recon.matchedPaymentId": payment_id,
                                    "reversal": {"$ne": True}}}},
        sort=[("statement.sequence", -1)])
    if stmt is None:
        return None
    line = next(e for e in reversed(stmt["entries"])
                if (e.get("recon") or {}).get("matchedPaymentId") == payment_id
                and not e.get("reversal"))
    return stmt, line


def correction_for(db, exception_id: str) -> Optional[dict]:
    return db["canonicalJsonStorage"].find_one(
        {"purpose": PURPOSE_ACCOUNT_STATEMENT, "statement.correction.exceptionId": exception_id})


def issue_corrected_statement(db, exception_id: str, *,
                              now: Optional[datetime] = None) -> dict:
    """Write the correspondent's correcting camt.053 for one answered investigation.

    Legal only once the correspondent has replied (camt.111) and the exception is still
    OPEN awaiting this statement. Idempotent per exception: a second call returns the first
    statement.
    """
    existing = correction_for(db, exception_id)
    if existing is not None:
        return existing
    exc = db["exceptions"].find_one({"exceptionId": exception_id})
    if exc is None:
        raise LookupError(f"Exception {exception_id} not found.")
    if exc.get("status") != "OPEN" or not exc.get("awaitingCorrectedStatement"):
        raise ValueError(f"Exception {exception_id} is not awaiting a corrected statement.")
    position = db["settlementPositions"].find_one(
        {"paymentId": exc["paymentId"]}, sort=[("createdAt", -1)])
    if position is None:
        raise ValueError(f"Payment {exc['paymentId']} has no settlement position.")
    account_code = position.get("settlementAccountCode") or NOSTRO_USD
    messages = db["canonicalJsonStorage"]
    found = _original_line(messages, exc["paymentId"])
    original_stmt, original = found if found else (None, None)
    if exc.get("category") == "RECONCILIATION_DISCREPANCY" and original is None:
        raise ValueError(f"No booked statement line to correct for {exc['paymentId']}.")
    original_ref = ({"paymentMessageId": original_stmt["paymentMessageId"],
                     "lineNo": original["lineNo"]} if original else None)
    entries = camt053.correction_entries(original, position, original_ref=original_ref)

    for _ in range(2):  # one retry: the periodic statement may take the window first
        now = now or datetime.now(timezone.utc)
        previous = _previous_statement(messages, account_code)
        window_from = _as_datetime(previous["statement"]["window"]["to"]) if previous else now
        sequence = previous["statement"]["sequence"] + 1 if previous else 1
        opening = previous["statement"]["closingBalance"] if previous else 0.0
        currency = entries[-1]["currency"]
        oid = ObjectId()
        message = camt053.build(
            statement_id=derive_ref("STMT", oid), account_code=account_code,
            currency=currency, window_from=window_from, window_to=now, sequence=sequence,
            opening_balance=opening, entries=entries, now=now)
        doc = statement_doc(
            oid=oid, message=message, account_code=account_code, currency=currency,
            window_from=window_from, window_to=now, sequence=sequence,
            opening_balance=opening,
            closing_balance=camt053.closing_balance(opening, entries),
            entries=entries, now=now)
        doc["statement"]["correction"] = {
            "exceptionId": exception_id,
            "caseId": (exc.get("escalation") or {}).get("caseId"),
            "paymentId": exc["paymentId"],
            "corrects": original_ref,
        }
        try:
            messages.insert_one(doc)
        except DuplicateKeyError:
            # Either a concurrent caller wrote this exception's correction (theirs wins),
            # or the periodic statement took the window first (retry on the next one).
            winner = correction_for(db, exception_id)
            if winner is not None:
                return winner
            now = None
            continue
        logger.info("corrected statement %s for %s (%s)",
                    doc["paymentMessageId"], exc["paymentId"], exception_id)
        return doc
    raise RuntimeError(f"Could not write the corrected statement for {exception_id}.")


def due_corrections(db, *, delay_seconds: int = CORRECTED_STATEMENT_SECONDS,
                    now: Optional[datetime] = None) -> list[str]:
    """Answered investigations whose simulated correcting statement is due."""
    from datetime import timedelta

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=delay_seconds)
    ids = [e["exceptionId"] for e in db["exceptions"].find(
        {"status": "OPEN", "awaitingCorrectedStatement": True,
         "escalation.reply.at": {"$lte": cutoff}}, {"exceptionId": 1})]
    return [i for i in ids if correction_for(db, i) is None]
