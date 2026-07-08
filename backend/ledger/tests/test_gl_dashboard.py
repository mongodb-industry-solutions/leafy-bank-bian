"""Unit tests for pipeline_read_service.get_gl_dashboard — hermetic, mocked.

The aggregation math itself runs in MongoDB, so these tests mock each
collection's aggregate() to return the shape MongoDB would produce and assert
the service assembles the response correctly (mapping, roll-ups, defaults).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from services import pipeline_read_service
from services.pipeline_read_service import get_gl_dashboard
from services.reconciliation_service import ReconciliationResult
from datetime import datetime, timezone


def _conn(aggregate_by_coll: dict):
    """Route get_collection(db, name) to a mock whose aggregate() yields the
    per-collection rows in aggregate_by_coll[name]."""
    def get_collection(_db, name):
        coll = MagicMock()
        coll.aggregate.return_value = iter(aggregate_by_coll.get(name, []))
        return coll
    conn = MagicMock()
    conn.get_collection.side_effect = get_collection
    return conn


def _recon(*matches):
    """Build ReconciliationResults; each arg True=reconciled."""
    now = datetime.now(timezone.utc)
    out = []
    for i, ok in enumerate(matches):
        out.append(ReconciliationResult(
            account_code=str(1000 + i),
            period_code="2025-05",
            subledger_sum=100,
            journal_sum=100 if ok else 99,
            checked_at=now,
        ))
    return out


def test_full_dashboard_happy_path(monkeypatch):
    aggregate_by_coll = {
        "journalEntries": [{
            "_id": None, "totalJournals": 24,
            "totalDebit": 12580000000, "totalCredit": 12580000000,
            "outOfBalance": 0, "currency": "USD",
        }],
        "ledgerEvents": [
            {"_id": "POSTED", "n": 22},
            {"_id": "PENDING", "n": 2},
        ],
        "subLedgerEntries": [
            {"accountCode": "11010", "accountName": "Cash",
             "debit": 4521000000, "credit": 4521000000, "balance": 0},
        ],
    }
    conn = _conn(aggregate_by_coll)
    monkeypatch.setattr(
        pipeline_read_service.reconciliation_service,
        "reconcile_all_accounts",
        lambda *a, **k: _recon(True, True, True),
    )

    result = get_gl_dashboard(conn, "test_db", period_code="2025-05")

    assert result["periodCode"] == "2025-05"
    assert result["summary"] == {
        "totalJournals": 24, "totalDebit": 12580000000,
        "totalCredit": 12580000000, "outOfBalance": 0, "currency": "USD",
    }
    assert result["journalStatus"] == {
        "posted": 22, "posting": 2, "failed": 0, "total": 24,
    }
    assert result["reconciliation"] == {
        "status": "BALANCED", "accountsChecked": 3, "breaks": 0,
    }
    assert result["topControlAccounts"] == [
        {"accountCode": "11010", "accountName": "Cash",
         "debit": 4521000000, "credit": 4521000000, "balance": 0},
    ]


def test_empty_period_returns_zeroed_summary(monkeypatch):
    conn = _conn({})  # no rows from any collection
    monkeypatch.setattr(
        pipeline_read_service.reconciliation_service,
        "reconcile_all_accounts",
        lambda *a, **k: [],
    )

    result = get_gl_dashboard(conn, "test_db", period_code="2099-01")

    assert result["summary"] == {
        "totalJournals": 0, "totalDebit": 0, "totalCredit": 0,
        "outOfBalance": 0, "currency": "USD",
    }
    assert result["journalStatus"] == {
        "posted": 0, "posting": 0, "failed": 0, "total": 0,
    }
    assert result["reconciliation"]["status"] == "BALANCED"
    assert result["reconciliation"]["accountsChecked"] == 0
    assert result["topControlAccounts"] == []


def test_reconciliation_break_flips_status(monkeypatch):
    conn = _conn({})
    monkeypatch.setattr(
        pipeline_read_service.reconciliation_service,
        "reconcile_all_accounts",
        lambda *a, **k: _recon(True, False, True),
    )

    result = get_gl_dashboard(conn, "test_db", period_code="2025-05")

    assert result["reconciliation"] == {
        "status": "OUT_OF_BALANCE", "accountsChecked": 3, "breaks": 1,
    }


def test_defaults_to_current_period(monkeypatch):
    conn = _conn({})
    monkeypatch.setattr(
        pipeline_read_service.reconciliation_service,
        "reconcile_all_accounts",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(pipeline_read_service, "_current_period", lambda: "2026-07")

    result = get_gl_dashboard(conn, "test_db")

    assert result["periodCode"] == "2026-07"
