"""GL read service — read-only queries for the FinancialAccounting API."""

from __future__ import annotations

from typing import Optional

from database.connection import MongoDBConnection


def get_gl_account(
    account_code: str,
    connection: MongoDBConnection,
    db_name: str,
) -> Optional[dict]:
    """Return the glAccounts doc for the given code, or None if not found."""
    coll = connection.get_collection(db_name, "glAccounts")
    return coll.find_one({"accountCode": account_code}, {"_id": 0})


def get_journal_entries_for_account(
    account_code: str,
    connection: MongoDBConnection,
    db_name: str,
    *,
    limit: int = 20,
    period_code: Optional[str] = None,
) -> list[dict]:
    """Return journal entries with a line for this account code, newest first."""
    jnl_coll = connection.get_collection(db_name, "journalEntries")
    match: dict = {"entries.accountCode": account_code}
    if period_code:
        match["periodCode"] = period_code
    pipeline = [
        {"$match": match},
        {"$sort": {"postingDate": -1}},
        {"$limit": limit},
        {"$project": {"_id": 0}},
    ]
    return list(jnl_coll.aggregate(pipeline))


def get_ledger_event(
    event_id: str,
    connection: MongoDBConnection,
    db_name: str,
) -> Optional[dict]:
    """Return the ledgerEvent doc for the given eventId, or None if not found."""
    coll = connection.get_collection(db_name, "ledgerEvents")
    return coll.find_one({"eventId": event_id}, {"_id": 0})
