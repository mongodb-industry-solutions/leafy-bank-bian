"""Reconciliation plan A4 — the two resolution actions that run on the ledger (D1a).

`RECHECK` and `LINK_STATEMENT_ENTRY` write only ledger-owned data: statement lines, the
settlement position's actual amount, the reconciliation item, and the exception's
resolution. The transactions service's `/resolve` refuses both with a pointer here.

Neither action decides that a payment is reconciled. Both end by re-running the
deterministic tie-out (`reconcile_payment`); an exception closes only when the engine
agrees (RECHECK) or when the pairing it records is the fact the exception was missing (LINK).
RECHECK moves no money and is fully reversible, which is why Part C's agent may call it
without operator approval.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from database.connection import MongoDBConnection
from services import reconciliation_service, statement_matching
from services.exceptions_service import (
    ACTION_LINK_STATEMENT_ENTRY,
    ACTION_RECHECK,
    CATEGORY_ORPHANED_SETTLEMENT,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_RECONCILIATION_MISSING,
    STATUS_OPEN,
    STATUS_RESOLVED,
)

logger = logging.getLogger(__name__)

OPERATOR = "payments-operations"
RECON_MANUAL_MATCHED = "MANUAL_MATCHED"


class ResolutionError(ValueError):
    """Base: the router maps the subclasses to 404 / 409 / 422."""


class NotFound(ResolutionError):
    pass


class Conflict(ResolutionError):
    pass


class NotLegal(ResolutionError):
    pass


def _open_exception(exc_coll, exception_id: str, categories: set[str], action: str) -> dict:
    exc = exc_coll.find_one({"exceptionId": exception_id})
    if exc is None:
        raise NotFound(f"Exception {exception_id} not found.")
    if exc["status"] != STATUS_OPEN:
        raise Conflict(f"Exception {exception_id} is {exc['status']}, not OPEN.")
    if exc["category"] not in categories:
        raise NotLegal(f"Action {action} is not legal for a {exc['category']} exception "
                       f"(legal on: {sorted(categories)}).")
    return exc


def _resolution(action: str, by: str, note: Optional[str], now: datetime) -> dict:
    return {"status": STATUS_RESOLVED,
            "resolution": {"action": action, "by": by, "at": now, "note": note},
            "updatedAt": now}


def recheck(connection: MongoDBConnection, db_name: str, exception_id: str, *,
            by: str = OPERATOR, note: Optional[str] = None) -> dict:
    """Match statements, queue orphans, re-run this payment's tie-out; resolve on RECONCILED.

    Returns `{outcome, check, exception}`. Anything short of RECONCILED leaves the exception
    OPEN — a recheck that finds nothing new is a normal answer, not an error.
    """
    exc_coll = connection.get_collection(db_name, "exceptions")
    exc = _open_exception(
        exc_coll, exception_id,
        {CATEGORY_RECONCILIATION_MISSING, CATEGORY_RECONCILIATION_DISCREPANCY}, ACTION_RECHECK)
    payment_id = exc["paymentId"]

    statement_matching.match_statements(connection, db_name)
    statement_matching.raise_orphans(connection, db_name)
    check, outcome = reconciliation_service.reconcile_payment(payment_id, connection, db_name)
    if check is None:
        raise NotFound(f"Payment {payment_id} for exception {exception_id} not found.")

    if outcome == reconciliation_service.RECONCILED:
        # Conditional on OPEN: the sweep's own MISSING auto-resolve may have just won.
        exc_coll.update_one({"_id": exc["_id"], "status": STATUS_OPEN},
                            {"$set": _resolution(ACTION_RECHECK, by, note, datetime.now(timezone.utc))})
    return {"outcome": outcome, "check": check.as_dict(),
            "exception": exc_coll.find_one({"exceptionId": exception_id}, {"_id": 0})}


def _find_line(messages, payment_message_id: str, line_no: int) -> tuple[dict, dict]:
    stmt = messages.find_one({"paymentMessageId": payment_message_id,
                              "purpose": statement_matching.STATEMENT_PURPOSE})
    if stmt is None:
        raise NotFound(f"Statement {payment_message_id} not found.")
    line = next((e for e in stmt.get("entries") or [] if e.get("lineNo") == line_no), None)
    if line is None:
        raise NotFound(f"Statement {payment_message_id} has no line {line_no}.")
    return stmt, line


def link(connection: MongoDBConnection, db_name: str, exception_id: str, *,
         payment_id: Optional[str] = None, payment_message_id: Optional[str] = None,
         line_no: Optional[int] = None, note: Optional[str] = None) -> dict:
    """Pair one unmatched statement line with one awaiting payment (LINK_STATEMENT_ENTRY).

    Called from either twin: an ORPHANED_SETTLEMENT (the line is known; pass `payment_id`)
    or a RECONCILIATION_MISSING (the payment is known; pass the line). Both twins close in
    one ACID txn with the line and position writes — claims first, so a racing resolver
    aborts the whole pairing. A pairing whose amounts differ is still recorded; the re-run
    then reads leg 2 MISMATCH and raises a DISCREPANCY (a re-keyed fee line), as intended.
    """
    exc_coll = connection.get_collection(db_name, "exceptions")
    messages = connection.get_collection(db_name, "paymentMessages")
    positions = connection.get_collection(db_name, "settlementPositions")
    payments = connection.get_collection(db_name, "payments")

    exc = _open_exception(
        exc_coll, exception_id,
        {CATEGORY_RECONCILIATION_MISSING, CATEGORY_ORPHANED_SETTLEMENT}, ACTION_LINK_STATEMENT_ENTRY)
    if exc["category"] == CATEGORY_ORPHANED_SETTLEMENT:
        subject = exc.get("subjectRef") or {}
        payment_message_id, line_no = subject.get("paymentMessageId"), subject.get("lineNo")
        if not payment_id:
            raise NotLegal("LINK on an orphan line needs the paymentId it belongs to.")
    else:
        payment_id = exc["paymentId"]
        if not payment_message_id or line_no is None:
            raise NotLegal("LINK on a missing payment needs the statement line (paymentMessageId, lineNo).")

    stmt, line = _find_line(messages, payment_message_id, line_no)
    recon = line.get("recon") or {}
    if recon.get("status") != statement_matching.RECON_UNMATCHED:
        raise Conflict(f"Line {line_no} of {payment_message_id} is already {recon.get('status')}.")

    payment = payments.find_one({"paymentId": payment_id}, {"_id": 0, "lifecycle": 1, "direction": 1})
    if payment is None:
        raise NotFound(f"Payment {payment_id} not found.")
    if not reconciliation_service.is_eligible(payment):
        raise Conflict(f"Payment {payment_id} is not awaiting reconciliation.")
    position = positions.find_one({"paymentId": payment_id}, sort=[("createdAt", -1)])
    if position is None:
        raise NotLegal(f"Payment {payment_id} has no settlement position to pair with.")
    if position.get("actualAmount") is not None:
        raise Conflict(f"Payment {payment_id} already holds a statement actual — nothing to link.")
    header = stmt.get("statement") or {}
    if header.get("accountCode") != position.get("settlementAccountCode"):
        raise NotLegal(f"Line is on nostro {header.get('accountCode')}, payment settled on "
                       f"{position.get('settlementAccountCode')}.")
    line_ccy = line.get("currency") or header.get("currency")
    if line_ccy != position.get("expectedCurrency", position.get("currency")):
        raise NotLegal(f"Line currency {line_ccy} differs from the payment's.")

    now = datetime.now(timezone.utc)
    line_exc_key = statement_matching.orphan_key(payment_message_id, line_no)
    twin_filter = (
        {"paymentId": payment_id, "category": CATEGORY_RECONCILIATION_MISSING, "status": STATUS_OPEN}
        if exc["category"] == CATEGORY_ORPHANED_SETTLEMENT else
        {"paymentId": line_exc_key, "category": CATEGORY_ORPHANED_SETTLEMENT, "status": STATUS_OPEN}
    )
    resolved = _resolution(ACTION_LINK_STATEMENT_ENTRY, OPERATOR, note, now)

    entries = stmt.get("entries") or []
    for e in entries:
        if e.get("lineNo") == line_no:
            e["recon"] = {**recon, "status": RECON_MANUAL_MATCHED, "matchedPaymentId": payment_id,
                          "matchedBy": OPERATOR, "at": now}

    with connection.client.start_session() as session:
        with session.start_transaction():
            claim = exc_coll.update_one({"_id": exc["_id"], "status": STATUS_OPEN},
                                        {"$set": resolved}, session=session)
            if not claim.matched_count:
                raise Conflict(f"Exception {exception_id} is no longer OPEN — another resolver acted.")
            # The twin may not exist yet (a MISSING whose window has not lapsed); best-effort.
            exc_coll.update_one(twin_filter, {"$set": resolved}, session=session)
            claimed = positions.update_one(
                {"_id": position["_id"], "actualAmount": None},
                {"$set": {"actualAmount": line.get("amount"), "actualCurrency": line_ccy,
                          "actualBookedAt": now, "sourceMessageRef": payment_message_id}},
                session=session)
            if not claimed.matched_count:
                raise Conflict(f"Payment {payment_id} was matched while linking.")
            messages.update_one({"_id": stmt["_id"]}, {"$set": {"entries": entries}}, session=session)

    logger.info("resolution: linked %s line %s to %s (%s)", payment_message_id, line_no,
                payment_id, exception_id)
    check, outcome = reconciliation_service.reconcile_payment(payment_id, connection, db_name)
    return {"outcome": outcome, "check": check.as_dict() if check else None,
            "exception": exc_coll.find_one({"exceptionId": exception_id}, {"_id": 0})}
