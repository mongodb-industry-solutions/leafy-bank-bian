"""Unit tests for the ingest worker pure functions.

Hermetic: inline CoA + account fixtures, no DB.
"""

from __future__ import annotations

from shared.coa_cache import ChartOfAccounts
from workers.ingest_worker import build_ledger_event


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



# --- Stage 9: reversal branch (doc 24 B5) -------------------------------------

def _coa_with_clearing() -> ChartOfAccounts:
    """A CoA that includes the 1131 wire-clearing leaf alongside customer deposits, so a
    reversal (Dr 1131 / Cr customer deposit) can be decomposed."""
    return ChartOfAccounts([
        {"accountCode": "1100", "accountName": "Clearing & Settlement", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": None},
        {"accountCode": "1130", "accountName": "Clearing & Settlement - Control", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "1100"},
        {"accountCode": "1131", "accountName": "Wire Clearing", "isPostingAccount": True, "status": "ACTIVE", "parentAccountCode": "1130"},
        {"accountCode": "2100", "accountName": "Customer Deposits", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": None},
        {"accountCode": "2110", "accountName": "Current Accounts - Control", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "2100"},
        {"accountCode": "2111", "accountName": "Personal Current Accounts", "isPostingAccount": True, "status": "ACTIVE", "parentAccountCode": "2110"},
    ])


def test_a_reversal_txn_builds_a_balanced_event_with_swapped_legs_and_reversalOf_set():
    """The compensating transactions doc carries `reversalOf`; ingest_worker detects it and
    posts the principal's legs SWAPPED: Dr the clearing account (1131) / Cr the customer
    deposit (2111) — the mirror image, so 1131 nets back to zero. Balanced by construction."""
    coa = _coa_with_clearing()
    debtor = _account("ACC-debtor", "2111")
    clearing = _account("ACC-CLEARING-WIRE", "1131")
    txn = _txn(
        reversalOf="TXN-original0001",
        payer={"accountId": "ACC-debtor"},     # same as original
        payee={"accountId": "ACC-CLEARING-WIRE"},
    )

    event = build_ledger_event(txn, payer_account=debtor, payee_account=clearing, coa=coa)

    # reversalOf is stamped from the transactions doc
    assert event["reversalOf"] == "TXN-original0001"
    # distinct idempotency key — a reversal is a second event, not a collision with the
    # principal (paymentId), fee (-FEE) or settlement (-SETTLEMENT)
    assert event["idempotencyKey"] == "PAY-abc12345-REV"
    # legs swapped vs the principal: Dr 1131 (clearing) / Cr 2111 (customer deposit)
    assert event["debitLeg"]["glAccountCode"] == "1131"
    assert event["creditLeg"]["glAccountCode"] == "2111"
    assert event["debitLeg"]["amount"] == event["creditLeg"]["amount"]   # balanced
    # mappingVersion bumped (1.3.0)
    from shared.posting_rules import MAPPING_VERSION
    assert event["mappingVersion"] == MAPPING_VERSION == "1.3.0"
    # eventType stays PAYMENT_PRINCIPAL (no invented enum value)
    assert event["eventType"] == "PAYMENT_PRINCIPAL"
    assert "reversal of TXN-original0001" in event["description"]


def test_the_reversal_branch_is_the_swap_of_the_principal_and_keys_off_the_txn_doc():
    """The reversal event's legs are exactly the principal's legs with DR/CR exchanged —
    proving the branch derives from the `transactions` doc's `reversalOf` + the accounts it
    names, never from `payments` (the firewall guard's concern)."""
    coa = _coa_with_clearing()
    debtor = _account("ACC-debtor", "2111")
    clearing = _account("ACC-CLEARING-WIRE", "1131")
    txn = _txn(payer={"accountId": "ACC-debtor"}, payee={"accountId": "ACC-CLEARING-WIRE"})

    principal = build_ledger_event(txn, debtor, clearing, coa)
    reversal = build_ledger_event({**txn, "reversalOf": "TXN-orig"}, debtor, clearing, coa)

    # principal: Dr debtor (2111) / Cr clearing (1131); reversal: swapped
    assert principal["debitLeg"]["glAccountCode"] == "2111"
    assert principal["creditLeg"]["glAccountCode"] == "1131"
    assert reversal["debitLeg"]["glAccountCode"] == "1131"
    assert reversal["creditLeg"]["glAccountCode"] == "2111"
    # same accounts, swapped sides — the reversal keys off `reversalOf` on the txn doc
    assert reversal["reversalOf"] == "TXN-orig"
    assert principal["reversalOf"] is None


def test_a_reversal_txn_does_not_build_a_fee_event():
    """The compensating doc zeroes feeAmount (a return moves the principal only), so
    build_fee_event returns None — no second fee leg is posted on a reversal."""
    from workers.ingest_worker import build_fee_event
    coa = _coa_with_clearing()
    debtor = _account("ACC-debtor", "2111")
    txn = _txn(reversalOf="TXN-orig", feeAmount=0.0, feeCurrency="USD",
               payer={"accountId": "ACC-debtor"}, payee={"accountId": "ACC-CLEARING-WIRE"})
    assert build_fee_event(txn, payer_account=debtor, coa=coa) is None
