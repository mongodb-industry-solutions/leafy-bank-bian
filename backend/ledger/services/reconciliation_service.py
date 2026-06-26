"""Reconciliation service — verify subledger ↔ journal integrity.

Implements the reconciliation expression:
  Σ subLedgerEntries WHERE controlAccountCode = X  ==  Σ journalEntries.entries[].amount WHERE accountCode = X

Both sums are signed (DR positive, CR negative) using the DR+/CR- convention throughout the
pipeline.

The subledger side is filtered to *journaled* rows only (status == POSTED AND journalEntryId
set). subLedgerEntries are born POSTED at projection (before the batch journal write, per design
Step 4.5), so filtering on status alone would count rows whose journal does not yet exist and
report a false mismatch during the projection→journal lag. journalEntryId is stamped at journal
write, so both sides move together and the check is trustworthy at any time. Note: this means a
row permanently stuck in projection (never journaled) is NOT flagged by this sum — that is a
separate "stuck rows" metric (see DN-2 in schema-bian-deviation.md), not a DR/CR imbalance.

Run period-scoped (period_code kwarg) for continuous monitoring; bounds the aggregation to
monthly volume via idx_period_code + idx_entries_account_code.
Run all-time (period_code=None) only for end-of-period audits — O(all-history).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from database.connection import MongoDBConnection

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def signed_amount(amount: int, side: str) -> int:
    """Apply DR+/CR- sign convention to an unsigned minor-unit amount."""
    return amount if side == "DEBIT" else -amount


@dataclass(frozen=True)
class ReconciliationResult:
    account_code: str
    period_code: Optional[str]   # None = all-time
    subledger_sum: int           # Σ SL signed (minor units, journaled rows only)
    journal_sum: int             # Σ JNL entries signed (minor units)
    checked_at: datetime

    @property
    def subledger_journal_match(self) -> bool:
        return self.subledger_sum == self.journal_sum

    @property
    def is_reconciled(self) -> bool:
        return self.subledger_journal_match


def _subledger_signed_sum(sl_coll, account_code: str, period_code: Optional[str]) -> int:
    """Σ subLedgerEntries.amount (signed, journaled only) for the given account + optional period."""
    match: dict = {
        "controlAccountCode": account_code,
        "status": "POSTED",
        "journalEntryId": {"$ne": ""},
    }
    if period_code:
        match["periodCode"] = period_code

    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": None,
            "signedSum": {"$sum": {"$cond": [
                {"$eq": ["$side", "DEBIT"]},
                "$amount",
                {"$multiply": [-1, "$amount"]},
            ]}},
        }},
    ]
    result = list(sl_coll.aggregate(pipeline))
    return int(result[0]["signedSum"]) if result else 0


def _journal_signed_sum(jnl_coll, account_code: str, period_code: Optional[str]) -> int:
    """Σ journalEntries.entries[].amount (signed) for the given accountCode + optional period.

    With period_code: hits idx_period_code first (document-level), then
    idx_entries_account_code on the unwound entries — aggregation is bounded to monthly volume.

    Without period_code: hits idx_entries_account_code only — O(all-history). Use for
    end-of-period audits, not continuous monitoring.
    """
    pipeline: list[dict] = []
    if period_code:
        pipeline.append({"$match": {"periodCode": period_code}})
    pipeline += [
        {"$unwind": "$entries"},
        {"$match": {"entries.accountCode": account_code}},
        {"$group": {
            "_id": None,
            "signedSum": {"$sum": {"$cond": [
                {"$eq": ["$entries.side", "DEBIT"]},
                "$entries.amount",
                {"$multiply": [-1, "$entries.amount"]},
            ]}},
        }},
    ]
    result = list(jnl_coll.aggregate(pipeline))
    return int(result[0]["signedSum"]) if result else 0


def reconcile_account(
    account_code: str,
    connection: MongoDBConnection,
    db_name: str,
    *,
    period_code: Optional[str] = None,
) -> ReconciliationResult:
    """Reconcile one account: subledger sum vs journal sum.

    Args:
        account_code: GL account code (e.g. "2000", "2100").
        period_code: "YYYY-MM" to scope to one period; None for all-time audit.
    """
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")
    jnl_coll = connection.get_collection(db_name, "journalEntries")

    sl_sum = _subledger_signed_sum(sl_coll, account_code, period_code)
    jnl_sum = _journal_signed_sum(jnl_coll, account_code, period_code)

    result = ReconciliationResult(
        account_code=account_code,
        period_code=period_code,
        subledger_sum=sl_sum,
        journal_sum=jnl_sum,
        checked_at=_now_utc(),
    )

    if not result.is_reconciled:
        logger.warning(
            "reconciliation FAIL account=%s period=%s sl=%d jnl=%d",
            account_code,
            period_code or "ALL-TIME",
            sl_sum,
            jnl_sum,
        )
    else:
        logger.debug(
            "reconciliation OK account=%s period=%s sum=%d",
            account_code,
            period_code or "ALL-TIME",
            sl_sum,
        )

    return result


def reconcile_all_accounts(
    connection: MongoDBConnection,
    db_name: str,
    *,
    period_code: Optional[str] = None,
) -> list[ReconciliationResult]:
    """Reconcile every posting account that has journaled subLedgerEntries in the given period."""
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")

    match: dict = {"status": "POSTED", "journalEntryId": {"$ne": ""}}
    if period_code:
        match["periodCode"] = period_code

    account_codes = sl_coll.distinct("controlAccountCode", match)
    return [
        reconcile_account(code, connection, db_name, period_code=period_code)
        for code in account_codes
    ]
