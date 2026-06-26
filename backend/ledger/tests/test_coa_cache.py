"""Unit tests for the ChartOfAccounts control-account helpers.

Hermetic: small inline 4-level tree, no DB.
"""

from __future__ import annotations

import pytest

from shared.coa_cache import ChartOfAccounts


def _coa() -> ChartOfAccounts:
    # 2111 (leaf, posting) -> 2110 (control, non-posting) -> 2100 (non-posting) -> 2000 (non-posting)
    return ChartOfAccounts([
        {"accountCode": "2000", "accountName": "Liabilities", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": None},
        {"accountCode": "2100", "accountName": "Customer Deposits", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "2000"},
        {"accountCode": "2110", "accountName": "Current Accounts - Control", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "2100"},
        {"accountCode": "2111", "accountName": "Personal Current Accounts", "isPostingAccount": True, "status": "ACTIVE", "parentAccountCode": "2110"},
        {"accountCode": "2199", "accountName": "Closed Control", "isPostingAccount": False, "status": "INACTIVE", "parentAccountCode": "2100"},
        {"accountCode": "9999", "accountName": "Orphan Leaf", "isPostingAccount": True, "status": "ACTIVE", "parentAccountCode": None},
    ])


# --- control_account_for ------------------------------------------------------

def test_control_account_is_nearest_non_posting_parent():
    assert _coa().control_account_for("2111") == "2110"


def test_control_account_unknown_code_raises():
    with pytest.raises(ValueError, match="not found"):
        _coa().control_account_for("0000")


def test_control_account_no_non_posting_ancestor_raises():
    with pytest.raises(ValueError, match="no control"):
        _coa().control_account_for("9999")


# --- require_active_control_account -------------------------------------------

def test_require_active_control_account_returns_doc():
    acct = _coa().require_active_control_account("2110")
    assert acct["accountCode"] == "2110"


def test_require_active_control_account_rejects_posting_leaf():
    with pytest.raises(ValueError, match="posting leaf"):
        _coa().require_active_control_account("2111")


def test_require_active_control_account_rejects_inactive():
    with pytest.raises(ValueError, match="not ACTIVE"):
        _coa().require_active_control_account("2199")


def test_require_active_control_account_unknown_code_raises():
    with pytest.raises(ValueError, match="not found"):
        _coa().require_active_control_account("0000")
