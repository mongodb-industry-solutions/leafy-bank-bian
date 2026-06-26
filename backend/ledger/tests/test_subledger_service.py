"""Unit tests for the subledger service pure builder.

Hermetic: inline ledgerEvent fixture, no DB.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.subledger_service import assign_running_balances, build_subledger_entries


def _event(**overrides) -> dict:
    base = {
        "eventId": "LE-abcd1234",
        "groupId": "GRP-abcd1234",
        "occurredAt": datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc),
        "meta": {"subLedgerType": "CUSTOMER_DEPOSITS", "periodCode": "2026-06"},
        "eventType": "PAYMENT_PRINCIPAL",
        "mappingVersion": "1.0.0",
        "debitLeg": {
            "glAccountCode": "2111",
            "controlAccountCode": "2110",
            "amount": 25000,
            "currency": "USD",
            "entityReference": {"entityType": "ACCOUNT", "entityId": "ACC-debtor"},
        },
        "creditLeg": {
            "glAccountCode": "2121",
            "controlAccountCode": "2120",
            "amount": 25000,
            "currency": "USD",
            "entityReference": {"entityType": "ACCOUNT", "entityId": "ACC-creditor"},
        },
    }
    base.update(overrides)
    return base


def test_produces_exactly_two_entries():
    entries = build_subledger_entries(_event())
    assert len(entries) == 2


def test_one_debit_one_credit():
    entries = build_subledger_entries(_event())
    sides = {e["side"] for e in entries}
    assert sides == {"DEBIT", "CREDIT"}


def test_idempotency_keys_differ_by_side():
    entries = build_subledger_entries(_event())
    keys = {e["idempotencyKey"] for e in entries}
    assert "LE-abcd1234-DEBIT" in keys
    assert "LE-abcd1234-CREDIT" in keys


def test_debit_entry_maps_correct_leg():
    entries = build_subledger_entries(_event())
    debit = next(e for e in entries if e["side"] == "DEBIT")
    assert debit["controlAccountCode"] == "2110"
    assert debit["entityReference"]["entityId"] == "ACC-debtor"
    assert debit["amount"] == 25000


def test_credit_entry_maps_correct_leg():
    entries = build_subledger_entries(_event())
    credit = next(e for e in entries if e["side"] == "CREDIT")
    assert credit["controlAccountCode"] == "2120"
    assert credit["entityReference"]["entityId"] == "ACC-creditor"


def test_entries_born_posted():
    entries = build_subledger_entries(_event())
    assert all(e["status"] == "POSTED" for e in entries)
    assert all(e["journalEntryId"] == "" for e in entries)


def test_subledger_ids_have_correct_prefix():
    entries = build_subledger_entries(_event())
    assert all(e["subLedgerId"].startswith("SL-") for e in entries)


def test_source_reference_points_to_ledger_event():
    entries = build_subledger_entries(_event())
    for e in entries:
        assert e["sourceReference"]["sourceCollection"] == "ledgerEvents"
        assert e["sourceReference"]["sourceId"] == "LE-abcd1234"


def test_source_reference_has_source_system_and_type():
    entries = build_subledger_entries(_event())
    for e in entries:
        assert e["sourceReference"]["sourceSystem"] == "LEDGER_PIPELINE"
        assert e["sourceReference"]["sourceType"] == "LEDGER_EVENT"


def test_period_code_derived_from_occurred_at():
    entries = build_subledger_entries(_event())
    for e in entries:
        assert e["periodCode"] == "2026-06"


def test_functional_amount_equals_amount():
    entries = build_subledger_entries(_event())
    for e in entries:
        assert e["functionalAmount"] == e["amount"]


def test_mapping_version_carried_from_event():
    entries = build_subledger_entries(_event(mappingVersion="1.0.0"))
    for e in entries:
        assert e["mappingVersion"] == "1.0.0"


def test_mapping_version_defaults_to_empty_string_when_absent():
    event = _event()
    del event["mappingVersion"]
    entries = build_subledger_entries(event)
    for e in entries:
        assert e["mappingVersion"] == ""


def test_updated_at_present_on_creation():
    entries = build_subledger_entries(_event())
    for e in entries:
        assert "updatedAt" in e
        assert e["updatedAt"] is not None


# --- assign_running_balances --------------------------------------------------

def _leg(side: str, code: str, amount: int) -> dict:
    return {"side": side, "controlAccountCode": code, "amount": amount}


def test_running_balance_same_account_transfer_nets_to_zero():
    # Both legs hit the same control account (deposit->deposit transfer): the credit leg must
    # see the debit leg's effect, so the account's last running balance is unchanged.
    entries = [_leg("DEBIT", "2100", 25000), _leg("CREDIT", "2100", 25000)]
    assign_running_balances(entries, {"2100": 1000000})

    debit = next(e for e in entries if e["side"] == "DEBIT")
    credit = next(e for e in entries if e["side"] == "CREDIT")
    # DEBIT-first chaining: 1,000,000 -> +25,000 -> 1,025,000 -> -25,000 -> 1,000,000.
    assert debit["runningBalance"] == 1025000
    assert credit["runningBalance"] == 1000000  # ending balance for the account


def test_running_balance_distinct_accounts_are_independent():
    entries = [_leg("DEBIT", "2100", 25000), _leg("CREDIT", "2200", 25000)]
    assign_running_balances(entries, {"2100": 100000, "2200": 300000})

    debit = next(e for e in entries if e["controlAccountCode"] == "2100")
    credit = next(e for e in entries if e["controlAccountCode"] == "2200")
    assert debit["runningBalance"] == 125000   # 100,000 + 25,000
    assert credit["runningBalance"] == 275000  # 300,000 - 25,000


def test_running_balance_defaults_missing_account_to_zero():
    entries = [_leg("DEBIT", "2100", 5000), _leg("CREDIT", "2100", 5000)]
    assign_running_balances(entries, {})
    credit = next(e for e in entries if e["side"] == "CREDIT")
    assert credit["runningBalance"] == 0  # 0 + 5,000 - 5,000
