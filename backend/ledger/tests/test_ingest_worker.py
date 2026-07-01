"""Unit tests for the ingest worker pure functions.

Hermetic: inline CoA + account fixtures, no DB.
"""

from __future__ import annotations

import pytest

from shared.coa_cache import ChartOfAccounts
from workers.ingest_worker import _derive_posting_mode, build_ledger_event


def _coa() -> ChartOfAccounts:
    # 4-level tree: leaves 2111/2121 (posting) under controls 2110/2120 (non-posting).
    return ChartOfAccounts([
        {"accountCode": "2100", "accountName": "Customer Deposits", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": None},
        {"accountCode": "2110", "accountName": "Current Accounts - Control", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "2100"},
        {"accountCode": "2111", "accountName": "Personal Current Accounts", "isPostingAccount": True, "status": "ACTIVE", "parentAccountCode": "2110"},
        {"accountCode": "2120", "accountName": "Savings Accounts - Control", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "2100"},
        {"accountCode": "2121", "accountName": "Personal Savings Accounts", "isPostingAccount": True, "status": "ACTIVE", "parentAccountCode": "2120"},
    ])


def _account(account_id: str, gl_code: str) -> dict:
    return {"accountId": account_id, "gl": {"accountCode": gl_code}}


def _txn(**overrides) -> dict:
    base = {
        "paymentId": "PAY-abc12345",
        "amount": 250.00,
        "currency": "USD",
        "rail": "ACH",
        "paymentType": "ACH_TRANSFER",
        "payer": {"accountId": "ACC-debtor"},
        "payee": {"accountId": "ACC-creditor"},
    }
    base.update(overrides)
    return base


# --- build_ledger_event -------------------------------------------------------

def test_event_has_one_doc_shape():
    event = build_ledger_event(
        _txn(),
        payer_account=_account("ACC-debtor", "2111"),
        payee_account=_account("ACC-creditor", "2121"),
        coa=_coa(),
    )
    assert "debitLeg" in event
    assert "creditLeg" in event
    assert "legs" not in event


def test_event_idempotency_key_is_payment_id():
    event = build_ledger_event(
        _txn(paymentId="PAY-xyz"),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert event["idempotencyKey"] == "PAY-xyz"


def test_debit_leg_maps_to_payer():
    event = build_ledger_event(
        _txn(),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert event["debitLeg"]["glAccountCode"] == "2111"
    assert event["debitLeg"]["controlAccountCode"] == "2110"
    assert event["debitLeg"]["entityReference"]["entityId"] == "ACC-debtor"


def test_credit_leg_maps_to_payee():
    event = build_ledger_event(
        _txn(),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert event["creditLeg"]["glAccountCode"] == "2121"
    assert event["creditLeg"]["controlAccountCode"] == "2120"
    assert event["creditLeg"]["entityReference"]["entityId"] == "ACC-creditor"


def test_amounts_in_minor_units():
    event = build_ledger_event(
        _txn(amount=1.50),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert event["debitLeg"]["amount"] == 150
    assert event["creditLeg"]["amount"] == 150


def test_balanced_legs():
    event = build_ledger_event(
        _txn(amount=99.99),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert event["debitLeg"]["amount"] == event["creditLeg"]["amount"]


def test_posting_status_is_pending():
    event = build_ledger_event(
        _txn(),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert event["postingStatus"] == "PENDING"
    assert event["postingResult"] is None


def test_source_reference_points_to_transaction():
    event = build_ledger_event(
        _txn(paymentId="PAY-ref1"),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert event["sourceReference"]["sourceCollection"] == "transactions"
    assert event["sourceReference"]["sourceId"] == "PAY-ref1"


def test_source_reference_has_source_system_and_type():
    event = build_ledger_event(
        _txn(),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert event["sourceReference"]["sourceSystem"] == "LEDGER_PIPELINE"
    assert event["sourceReference"]["sourceType"] == "PAYMENT"


def test_meta_has_source_system_period_name():
    event = build_ledger_event(
        _txn(),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert event["meta"]["sourceSystem"] == "LEDGER_PIPELINE"
    assert "periodName" not in event["meta"]
    # periodName is top-level per BIAN FinancialBookingLogPeriodName
    assert "periodName" in event
    assert event["periodName"] != event["meta"]["periodCode"]


def test_event_has_value_date_equal_to_occurred_at():
    event = build_ledger_event(
        _txn(),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert event["valueDate"] == event["occurredAt"]


def test_event_has_description():
    event = build_ledger_event(
        _txn(),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert "description" in event
    assert event["description"]  # non-empty
    assert "PAY-abc12345" in event["description"]


def test_event_id_and_group_id_have_correct_prefixes():
    event = build_ledger_event(
        _txn(),
        _account("ACC-debtor", "2111"),
        _account("ACC-creditor", "2121"),
        _coa(),
    )
    assert event["eventId"].startswith("LE-")
    assert event["groupId"].startswith("GRP-")


# --- _derive_posting_mode -----------------------------------------------------

@pytest.mark.parametrize("rail,ptype,expected", [
    ("WIRE", None, "REALTIME"),
    ("wire", None, "REALTIME"),
    (None, "WIRE_TRANSFER", "REALTIME"),
    ("INTERNAL", None, "REALTIME"),
    ("ACH", None, "BATCH"),
    (None, "ACH", "BATCH"),
    ("VENMO", None, "BATCH"),
    ("PAYPAL", None, "BATCH"),
    ("RTP", "OTHER", "BATCH"),
    (None, None, "BATCH"),
])
def test_derive_posting_mode(rail, ptype, expected):
    assert _derive_posting_mode(rail, ptype) == expected
