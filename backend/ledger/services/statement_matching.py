"""Reconciliation plan A2 — match camt.053 statement lines to settlement positions.

The correspondent's statement (`paymentMessages`, `purpose: ACCOUNT_STATEMENT`, written by
the transactions service's gateway) is the only source of `settlementPositions.actualAmount`
for an outbound wire. Leg 2 of reconciliation then compares our settlement posting against
an external record instead of a figure we wrote ourselves (plan Decision 1).

Match rule (A2 decision D1a, Kiran 2026-09-30): a line matches a position when
`line.reference == position.paymentId`, the statement's account is the position's
`settlementAccountCode`, and the currencies agree — **whatever the amount**. `paymentId` is
a unique key, so this is an exact match, not a fuzzy one. The amount is recorded as found:
an equal amount is `EXACT_KEY`; a different one is `REFERENCE_ONLY`, and leg 2 reports the
delta (FEE_DEDUCTED's 24,975 vs 25,000). Anything whose reference does not resolve — a
re-keyed reference, an orphan — stays `UNMATCHED`, and `raise_orphans` (plan A3) queues it
as `ORPHANED_SETTLEMENT` for the operator and the agent.

Never reads the lines' `simulatedPaymentId` / `simulatedOutcome`: they are the simulator's
truth, and a real statement does not tell you which payment a line is (a guard test pins it).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from database.connection import MongoDBConnection
from services.exceptions_service import (
    CATEGORY_ORPHANED_SETTLEMENT,
    SERVICE_LEDGER,
    SOURCE_STAGE_RECONCILE,
    SUBJECT_STATEMENT_LINE,
    record_exception,
)

logger = logging.getLogger(__name__)

STATEMENT_PURPOSE = "ACCOUNT_STATEMENT"  # mirrored from transactions' inbound_documents
RECON_UNMATCHED = "UNMATCHED"
RECON_AUTO_MATCHED = "AUTO_MATCHED"
MATCHED_BY_EXACT_KEY = "EXACT_KEY"
MATCHED_BY_REFERENCE_ONLY = "REFERENCE_ONLY"

# Plan B: backfilled statements (`statement.historical`) are agent evidence — precedents and
# correspondent lag — never live books, so neither matching nor orphan-raising reads them.
_LIVE_UNMATCHED = {"purpose": STATEMENT_PURPOSE, "entries.recon.status": RECON_UNMATCHED,
                   "statement.historical": {"$ne": True}}


def _to_minors(amount) -> Optional[int]:
    return None if amount is None else round(float(amount) * 100)


def _match_line(positions_coll, account_code: str, currency: str, line: dict) -> Optional[dict]:
    """The position this line settles, or None. A position already holding an actual amount
    is not re-matched — the first statement that books a payment is the one that counts."""
    position = positions_coll.find_one(
        {"paymentId": line.get("reference"), "settlementAccountCode": account_code},
        sort=[("createdAt", -1)],
    )
    if position is None or position.get("actualAmount") is not None:
        return None
    if (line.get("currency") or currency) != position.get("expectedCurrency", position.get("currency")):
        return None
    return position


def match_statements(connection: MongoDBConnection, db_name: str,
                     now: Optional[datetime] = None) -> dict:
    """Match every UNMATCHED line on every statement. Idempotent: a matched line and a
    position that already holds an actual amount are both skipped on the next run."""
    now = now or datetime.now(timezone.utc)
    messages = connection.get_collection(db_name, "paymentMessages")
    positions = connection.get_collection(db_name, "settlementPositions")

    result = {"lines": 0, "matched": 0, "unmatched": 0}
    for stmt in messages.find(_LIVE_UNMATCHED):
        header = stmt.get("statement") or {}
        account_code = header.get("accountCode")
        entries = stmt.get("entries") or []
        changed = False
        for line in entries:
            if (line.get("recon") or {}).get("status") != RECON_UNMATCHED:
                continue
            result["lines"] += 1
            position = _match_line(positions, account_code, header.get("currency"), line)
            if position is None:
                result["unmatched"] += 1
                continue
            same_amount = _to_minors(line.get("amount")) == _to_minors(position.get("expectedAmount"))
            # Conditional on `actualAmount: None` so a racing matcher cannot overwrite.
            claimed = positions.update_one(
                {"_id": position["_id"], "actualAmount": None},
                {"$set": {
                    "actualAmount": line.get("amount"),
                    "actualCurrency": line.get("currency") or header.get("currency"),
                    "actualBookedAt": now,
                    "sourceMessageRef": stmt.get("paymentMessageId"),
                }},
            )
            if not claimed.matched_count:
                result["unmatched"] += 1
                continue
            line["recon"] = {
                "status": RECON_AUTO_MATCHED,
                "matchedPaymentId": position["paymentId"],
                "matchedBy": MATCHED_BY_EXACT_KEY if same_amount else MATCHED_BY_REFERENCE_ONLY,
                "at": now,
            }
            changed = True
            result["matched"] += 1
        if changed:
            messages.update_one({"_id": stmt["_id"]}, {"$set": {"entries": entries}})
    if result["lines"]:
        logger.info("statement matching: %s", result)
    return result


def orphan_key(payment_message_id: str, line_no: int) -> str:
    """The `exceptions.paymentId` an orphan line is queued under (plan A3 D1a). An orphan has
    no payment; keying it per line lets the OPEN-unique index dedupe it unchanged."""
    return f"{payment_message_id}#{line_no}"


def raise_orphans(connection: MongoDBConnection, db_name: str) -> dict:
    """Queue every still-UNMATCHED statement line as `ORPHANED_SETTLEMENT` (plan A3).

    Runs right after `match_statements`. Raised on the first pass that leaves a line
    unmatched (D3): matching fails only on a reference that resolves to no position, and no
    later payment can have been on an earlier statement, so waiting would not heal it. A
    re-keyed line therefore pairs with its payment's later RECONCILIATION_MISSING — the twin
    A4's LINK_STATEMENT_ENTRY closes. The line is stamped `recon.exceptionId`, so the next
    pass skips it without touching `exceptions`.
    """
    messages = connection.get_collection(db_name, "paymentMessages")
    exc_coll = connection.get_collection(db_name, "exceptions")

    raised = 0
    for stmt in messages.find(_LIVE_UNMATCHED):
        header = stmt.get("statement") or {}
        message_id = stmt.get("paymentMessageId")
        entries = stmt.get("entries") or []
        changed = False
        for line in entries:
            recon = line.get("recon") or {}
            if recon.get("status") != RECON_UNMATCHED or recon.get("exceptionId"):
                continue
            line_no = line.get("lineNo")
            exc = record_exception(
                exc_coll, orphan_key(message_id, line_no), CATEGORY_ORPHANED_SETTLEMENT,
                {
                    "discrepancyAmount": None,
                    "discrepancyReason": "Correspondent statement line with no matching payment.",
                    "expectedAmount": None,
                    "actualAmount": line.get("amount"),
                    "returnCode": None,
                    "duplicateOf": None,
                    "reference": line.get("reference"),
                    "currency": line.get("currency") or header.get("currency"),
                    "bookingDate": (header.get("window") or {}).get("to"),
                },
                source={"stage": SOURCE_STAGE_RECONCILE, "service": SERVICE_LEDGER},
                subject_ref={"kind": SUBJECT_STATEMENT_LINE, "paymentMessageId": message_id,
                             "lineNo": line_no},
            )
            line["recon"] = {**recon, "exceptionId": exc["exceptionId"]}
            changed = True
            raised += 1
        if changed:
            messages.update_one({"_id": stmt["_id"]}, {"$set": {"entries": entries}})
    if raised:
        logger.info("statement matching: %d orphan line(s) queued", raised)
    return {"orphansRaised": raised}
