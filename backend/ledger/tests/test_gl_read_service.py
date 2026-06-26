"""Unit tests for gl_read_service — hermetic, mocked MongoDBConnection."""

from __future__ import annotations

from unittest.mock import MagicMock

from services.gl_read_service import (
    get_gl_account,
    get_journal_entries_for_account,
    get_ledger_event,
)


def _conn(find_one_return=None, aggregate_return=None):
    coll = MagicMock()
    coll.find_one.return_value = find_one_return
    coll.aggregate.return_value = iter(aggregate_return or [])
    conn = MagicMock()
    conn.get_collection.return_value = coll
    return conn


# --- get_gl_account -----------------------------------------------------------

def test_get_gl_account_returns_none_when_not_found():
    result = get_gl_account("9999", _conn(find_one_return=None), "test_db")
    assert result is None


def test_get_gl_account_returns_doc_when_found():
    doc = {"accountCode": "2100", "accountName": "Customer Demand Deposits"}
    conn = _conn(find_one_return=doc)
    result = get_gl_account("2100", conn, "test_db")
    assert result == doc


def test_get_gl_account_queries_correct_collection():
    conn = _conn(find_one_return=None)
    get_gl_account("2100", conn, "test_db")
    conn.get_collection.assert_called_with("test_db", "glAccounts")


# --- get_ledger_event ---------------------------------------------------------

def test_get_ledger_event_returns_none_when_not_found():
    result = get_ledger_event("LE-abcd1234", _conn(find_one_return=None), "test_db")
    assert result is None


def test_get_ledger_event_returns_doc_when_found():
    doc = {"eventId": "LE-abcd1234", "groupId": "GRP-xyz"}
    conn = _conn(find_one_return=doc)
    result = get_ledger_event("LE-abcd1234", conn, "test_db")
    assert result == doc


def test_get_ledger_event_queries_correct_collection():
    conn = _conn(find_one_return=None)
    get_ledger_event("LE-abcd1234", conn, "test_db")
    conn.get_collection.assert_called_with("test_db", "ledgerEvents")


# --- get_journal_entries_for_account ------------------------------------------

def test_get_journal_entries_returns_empty_list_when_none():
    result = get_journal_entries_for_account("2100", _conn(aggregate_return=[]), "test_db")
    assert result == []


def test_get_journal_entries_returns_list_of_docs():
    doc = {"journalId": "JNL-abcd1234", "entries": []}
    conn = _conn(aggregate_return=[doc])
    result = get_journal_entries_for_account("2100", conn, "test_db")
    assert result == [doc]


def test_get_journal_entries_queries_correct_collection():
    conn = _conn(aggregate_return=[])
    get_journal_entries_for_account("2100", conn, "test_db")
    conn.get_collection.assert_called_with("test_db", "journalEntries")
