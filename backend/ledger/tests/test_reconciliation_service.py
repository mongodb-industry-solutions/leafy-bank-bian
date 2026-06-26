"""Unit tests for the reconciliation service pure functions.

Hermetic: no DB. Tests cover signed_amount and ReconciliationResult properties.
DB-dependent functions (reconcile_account, reconcile_all_accounts) require integration
tests against a live fin_migration DB.
"""

from __future__ import annotations

from datetime import datetime, timezone

from services.reconciliation_service import ReconciliationResult, signed_amount


# --- signed_amount ------------------------------------------------------------

def test_signed_amount_debit_is_positive():
    assert signed_amount(10000, "DEBIT") == 10000


def test_signed_amount_credit_is_negative():
    assert signed_amount(10000, "CREDIT") == -10000


def test_signed_amount_zero_is_zero():
    assert signed_amount(0, "DEBIT") == 0
    assert signed_amount(0, "CREDIT") == 0


# --- ReconciliationResult properties -----------------------------------------

_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)


def _result(
    sl_sum: int,
    jnl_sum: int,
    period_code: str | None = "2026-06",
) -> ReconciliationResult:
    return ReconciliationResult(
        account_code="2100",
        period_code=period_code,
        subledger_sum=sl_sum,
        journal_sum=jnl_sum,
        checked_at=_NOW,
    )


def test_subledger_journal_match_when_equal():
    assert _result(50000, 50000).subledger_journal_match is True


def test_subledger_journal_mismatch_when_not_equal():
    assert _result(50000, 49999).subledger_journal_match is False


# --- is_reconciled ------------------------------------------------------------

def test_reconciled_when_sl_eq_jnl():
    assert _result(50000, 50000).is_reconciled is True


def test_not_reconciled_when_sl_ne_jnl():
    assert _result(50000, 49999).is_reconciled is False


# --- negative sums (credit-normal accounts) -----------------------------------

def test_reconciled_with_negative_sums():
    assert _result(-200000, -200000).is_reconciled is True


def test_not_reconciled_with_negative_sums_mismatch():
    assert _result(-200000, -199000).is_reconciled is False


# --- all-time (period_code=None) ----------------------------------------------

def test_all_time_result_has_none_period_code():
    r = _result(50000, 50000, period_code=None)
    assert r.period_code is None
    assert r.is_reconciled is True
