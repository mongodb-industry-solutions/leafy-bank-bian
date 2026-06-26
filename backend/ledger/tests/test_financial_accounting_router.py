"""Unit tests for the FinancialAccounting router handlers.

Calls handler functions directly with a mocked Request — no httpx required.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from routers.financial_accounting import retrieve_financial_accounting, retrieve_ledger_posting


def _request(db_name: str = "test_db") -> MagicMock:
    req = MagicMock()
    req.app.state.connection = MagicMock()
    req.app.state.db_name = db_name
    return req


_GL_ACCOUNT = {
    "accountCode": "2100",
    "accountName": "Customer Demand Deposits",
    "accountType": "LIABILITY",
    "normalBalance": "CREDIT",
    "isPostingAccount": True,
}

_LEDGER_EVENT = {
    "eventId": "LE-abcd1234",
    "groupId": "GRP-xyz",
    "eventType": "PAYMENT_PRINCIPAL",
    "postingStatus": "POSTED",
}


# --- retrieve_financial_accounting -------------------------------------------

def test_retrieve_fa_raises_404_when_account_not_found():
    with patch("routers.financial_accounting.gl_read_service.get_gl_account", return_value=None):
        with pytest.raises(HTTPException) as exc:
            retrieve_financial_accounting("9999", _request())
    assert exc.value.status_code == 404
    assert exc.value.detail == "GL account 9999 not found"


def test_retrieve_fa_returns_json_response_with_account_and_journals():
    journals = [{"journalId": "JNL-abcd1234", "entries": []}]
    with (
        patch("routers.financial_accounting.gl_read_service.get_gl_account", return_value=_GL_ACCOUNT),
        patch("routers.financial_accounting.gl_read_service.get_journal_entries_for_account", return_value=journals),
    ):
        response = retrieve_financial_accounting("2100", _request())
    body = json.loads(response.body)
    assert body["financialAccountingId"] == "2100"
    assert body["glAccount"]["accountCode"] == "2100"
    assert len(body["recentJournals"]) == 1


def test_retrieve_fa_passes_period_code_to_service():
    with (
        patch("routers.financial_accounting.gl_read_service.get_gl_account", return_value=_GL_ACCOUNT),
        patch(
            "routers.financial_accounting.gl_read_service.get_journal_entries_for_account",
            return_value=[],
        ) as mock_journals,
    ):
        retrieve_financial_accounting("2100", _request(), period_code="2026-06")
    _, kwargs = mock_journals.call_args
    assert kwargs["period_code"] == "2026-06"


# --- retrieve_ledger_posting --------------------------------------------------

def test_retrieve_ledger_posting_raises_404_when_not_found():
    with patch("routers.financial_accounting.gl_read_service.get_ledger_event", return_value=None):
        with pytest.raises(HTTPException) as exc:
            retrieve_ledger_posting("2100", "LE-missing", _request())
    assert exc.value.status_code == 404
    assert exc.value.detail == "LedgerPosting LE-missing not found"


def test_retrieve_ledger_posting_returns_event_doc():
    with patch("routers.financial_accounting.gl_read_service.get_ledger_event", return_value=_LEDGER_EVENT):
        response = retrieve_ledger_posting("2100", "LE-abcd1234", _request())
    body = json.loads(response.body)
    assert body["eventId"] == "LE-abcd1234"
