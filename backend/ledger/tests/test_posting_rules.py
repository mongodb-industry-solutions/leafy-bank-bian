"""Unit tests for the principal-only posting rules.

Hermetic: an inline ChartOfAccounts fixture, no DB. One extra test cross-checks the rules
against the real canonical sample data (accounts.gl.accountCode -> CoA posting leaf) when
that data is reachable in the workspace; it skips otherwise.
"""

import json
from pathlib import Path

import pytest

from shared.coa_cache import ChartOfAccounts
from shared.posting_rules import (
    EVENT_PAYMENT_PRINCIPAL,
    SIDE_CREDIT,
    SIDE_DEBIT,
    PostingLeg,
    assert_balanced,
    decompose_principal_payment,
    to_minor_units,
)


# --- fixtures -----------------------------------------------------------------

def _coa() -> ChartOfAccounts:
    """Minimal CoA mirroring the real codes: deposit leaves + a non-posting parent."""
    return ChartOfAccounts([
        {"accountCode": "2000", "accountName": "Customer Deposits", "isPostingAccount": False},
        {"accountCode": "2100", "accountName": "Customer Deposits - Current", "isPostingAccount": True, "status": "ACTIVE"},
        {"accountCode": "2200", "accountName": "Customer Deposits - Savings", "isPostingAccount": True, "status": "ACTIVE"},
        {"accountCode": "4200", "accountName": "Fee Income", "isPostingAccount": True, "status": "ACTIVE"},
    ])


def _account(account_id: str, gl_code: str | None) -> dict:
    gl = {"accountCode": gl_code} if gl_code is not None else {}
    return {"accountId": account_id, "gl": gl}


# --- decomposition ------------------------------------------------------------

def test_principal_payment_sides_accounts_and_amount():
    legs = decompose_principal_payment(
        amount=250.00,
        currency="USD",
        debtor_account=_account("ACC-debtor", "2100"),
        creditor_account=_account("ACC-creditor", "2200"),
        coa=_coa(),
    )
    assert len(legs) == 2

    debit = next(leg for leg in legs if leg.side == SIDE_DEBIT)
    credit = next(leg for leg in legs if leg.side == SIDE_CREDIT)

    assert debit.event_type == EVENT_PAYMENT_PRINCIPAL
    assert debit.gl_account_code == "2100"          # debtor's own control account
    assert debit.account_id == "ACC-debtor"
    assert credit.gl_account_code == "2200"         # creditor's own control account
    assert credit.account_id == "ACC-creditor"
    assert debit.amount_minor == credit.amount_minor == 25000
    assert debit.currency == credit.currency == "USD"


def test_internal_transfer_nets_to_zero_on_same_control_account():
    # CURRENT -> CURRENT: both legs hit 2100, netting to zero on the control account.
    legs = decompose_principal_payment(
        amount=100.00, currency="USD",
        debtor_account=_account("ACC-a", "2100"),
        creditor_account=_account("ACC-b", "2100"),
        coa=_coa(),
    )
    by_code_signed = {}
    for leg in legs:
        sign = 1 if leg.side == SIDE_DEBIT else -1
        by_code_signed[leg.gl_account_code] = by_code_signed.get(leg.gl_account_code, 0) + sign * leg.amount_minor
    assert by_code_signed["2100"] == 0


def test_decomposition_is_balanced():
    legs = decompose_principal_payment(
        amount=42.50, currency="USD",
        debtor_account=_account("ACC-a", "2100"),
        creditor_account=_account("ACC-b", "2200"),
        coa=_coa(),
    )
    assert_balanced(legs)  # raises if not


# --- money conversion ---------------------------------------------------------

@pytest.mark.parametrize("amount,expected", [
    (250.00, 25000),
    (99.99, 9999),
    (0.1, 10),
    (1, 100),
    ("250.00", 25000),
])
def test_to_minor_units(amount, expected):
    assert to_minor_units(amount) == expected


@pytest.mark.parametrize("amount,expected", [
    (250.005, 25000),   # 25000.5 -> nearest even -> 25000
    (250.015, 25002),   # 25001.5 -> nearest even -> 25002
])
def test_to_minor_units_bankers_rounding(amount, expected):
    assert to_minor_units(amount) == expected


# --- guards / error paths -----------------------------------------------------

def test_unbalanced_legs_raise():
    legs = [
        PostingLeg(EVENT_PAYMENT_PRINCIPAL, SIDE_DEBIT, "2100", 100, "USD", "ACC-a"),
        PostingLeg(EVENT_PAYMENT_PRINCIPAL, SIDE_CREDIT, "2200", 99, "USD", "ACC-b"),
    ]
    with pytest.raises(ValueError, match="unbalanced"):
        assert_balanced(legs)


def test_non_posting_account_rejected():
    with pytest.raises(ValueError, match="not a posting account"):
        decompose_principal_payment(
            amount=10.0, currency="USD",
            debtor_account=_account("ACC-a", "2000"),   # parent, not a leaf
            creditor_account=_account("ACC-b", "2200"),
            coa=_coa(),
        )


def test_unknown_gl_account_rejected():
    with pytest.raises(ValueError, match="not found"):
        decompose_principal_payment(
            amount=10.0, currency="USD",
            debtor_account=_account("ACC-a", "9999"),   # not in CoA
            creditor_account=_account("ACC-b", "2200"),
            coa=_coa(),
        )


def test_missing_gl_account_code_rejected():
    with pytest.raises(ValueError, match="no gl.accountCode"):
        decompose_principal_payment(
            amount=10.0, currency="USD",
            debtor_account=_account("ACC-a", None),
            creditor_account=_account("ACC-b", "2200"),
            coa=_coa(),
        )


@pytest.mark.parametrize("amount", [0, 0.0, -5.0])
def test_non_positive_amount_rejected(amount):
    with pytest.raises(ValueError, match="positive"):
        decompose_principal_payment(
            amount=amount, currency="USD",
            debtor_account=_account("ACC-a", "2100"),
            creditor_account=_account("ACC-b", "2200"),
            coa=_coa(),
        )


# --- referential integrity against the real canonical sample data -------------

def _find_sample_dir() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "requirements" / "sample-data"
        if (candidate / "leafy_bank_bian.glAccounts.json").exists():
            return candidate
    return None


def test_every_sample_account_gl_code_is_a_posting_leaf():
    """Drift guard: every accounts.gl.accountCode resolves to a CoA posting leaf.

    Replaces a hardcoded account-type -> control-code table — this asserts the live data
    is internally consistent instead of duplicating the mapping.
    """
    sample_dir = _find_sample_dir()
    if sample_dir is None:
        pytest.skip("canonical sample-data not reachable from the test tree")

    gl_accounts = json.loads((sample_dir / "leafy_bank_bian.glAccounts.json").read_text())
    accounts = json.loads((sample_dir / "leafy_bank_bian.accounts.json").read_text())
    coa = ChartOfAccounts(gl_accounts)

    for acct in accounts:
        code = (acct.get("gl") or {}).get("accountCode")
        assert code, f"account {acct.get('accountId')} has no gl.accountCode"
        assert coa.is_posting_account(code), (
            f"account {acct.get('accountId')} -> gl.accountCode {code} is not a CoA posting leaf"
        )