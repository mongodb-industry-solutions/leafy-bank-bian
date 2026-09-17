"""Stage 7 step 5 gate — the settlement ledgerEvent (doc 21 B2).

Tests the pure functions: `decompose_settlement` and `build_settlement_event`.
The worker's change-stream loop is not tested here (it requires a live replica set);
its correctness is verified by the manual gate — one real external wire where
`journalEntries` still appears after the GL batch, for both the principal and the
settlement events.
"""

import pytest

from shared.coa_cache import ChartOfAccounts
from shared.posting_rules import (
    EVENT_PAYMENT_SETTLEMENT,
    SIDE_CREDIT,
    SIDE_DEBIT,
    decompose_settlement,
)
from workers.settlement_worker import build_settlement_event


# --- a minimal CoA stub that validates posting leaves --------------------------

class _StubCoA:
    """Just enough of ChartOfAccounts for the decomposition functions.

    `require_active_posting_account` is a no-op (the codes are trusted), and
    `control_account_for` returns a synthetic control. The real CoA reads from the
    DB; this stub keeps the test hermetic.
    """

    _CONTROLS = {"1131": "1130", "1111": "1110", "1121": "1120"}

    def require_active_posting_account(self, code: str) -> None:
        if code not in self._CONTROLS:
            raise ValueError(f"{code!r} not in the stub CoA")

    def control_account_for(self, code: str) -> str:
        return self._CONTROLS.get(code, "UNKNOWN")


_COA = _StubCoA()

_CLEARING_ACCOUNT = {
    "accountId": "ACC-CLEARING-WIRE",
    "type": "NOSTRO",
    "gl": {"accountCode": "1131"},
}

_PAYMENT = {
    "paymentId": "PAY-test0001",
    "amount": 250.0,
    "currency": "USD",
    "paymentType": "CREDIT_TRANSFER",
    "rail": "WIRE",
    "clearing": {
        "settlementAccountCode": "1111",
        "settledAt": "2026-09-02T15:30:00.000Z",
    },
}

_TXN = {
    "paymentId": "PAY-test0001",
    "txnId": "TXN-test0001",
    "amount": 250.0,
    "currency": "USD",
}


# --- decompose_settlement -----------------------------------------------------

def test_settlement_legs_are_balanced():
    """B2 — the settlement event is a balanced Dr/Cr pair, one document."""
    legs = decompose_settlement(
        amount=250.0, currency="USD",
        clearing_account=_CLEARING_ACCOUNT,
        settlement_account_code="1111",
        coa=_COA,
    )
    assert len(legs) == 2
    debit = next(l for l in legs if l.side == SIDE_DEBIT)
    credit = next(l for l in legs if l.side == SIDE_CREDIT)
    assert debit.gl_account_code == "1131"  # Wire Clearing
    assert credit.gl_account_code == "1111"  # Nostro Accounts
    assert debit.amount_minor == credit.amount_minor == 25000
    assert debit.event_type == EVENT_PAYMENT_SETTLEMENT


def test_central_bank_model_credits_reserves():
    """B5 model 2 — Cr 1121 Minimum Reserve Requirements."""
    legs = decompose_settlement(
        amount=250.0, currency="USD",
        clearing_account=_CLEARING_ACCOUNT,
        settlement_account_code="1121",
        coa=_COA,
    )
    credit = next(l for l in legs if l.side == SIDE_CREDIT)
    assert credit.gl_account_code == "1121"


def test_both_gl_codes_are_validated():
    """A missing or non-posting GL code raises — protects the projection worker."""
    with pytest.raises(ValueError, match="9999"):
        decompose_settlement(
            amount=250.0, currency="USD",
            clearing_account=_CLEARING_ACCOUNT,
            settlement_account_code="9999",
            coa=_COA,
        )


# --- build_settlement_event ---------------------------------------------------

def test_the_settlement_event_has_the_right_idempotency_key():
    """B2 — the principal's key stays exactly `{paymentId}`; the settlement's is `-SETTLEMENT`."""
    event = build_settlement_event(_PAYMENT, _TXN, _CLEARING_ACCOUNT, _COA)
    assert event["idempotencyKey"] == "PAY-test0001-SETTLEMENT"


def test_the_settlement_event_sources_from_payments_not_transactions():
    """The settlement event is sourced from `payments`, not `transactions` (doc 21 B2)."""
    event = build_settlement_event(_PAYMENT, _TXN, _CLEARING_ACCOUNT, _COA)
    assert event["sourceReference"]["sourceCollection"] == "payments"
    assert event["sourceReference"]["sourceType"] == "SETTLEMENT"


def test_the_settlement_event_carries_the_mapping_version():
    """Provenance: the MAPPING_VERSION bump (1.2.0) is stamped on the event."""
    from shared.posting_rules import MAPPING_VERSION
    event = build_settlement_event(_PAYMENT, _TXN, _CLEARING_ACCOUNT, _COA)
    assert event["mappingVersion"] == MAPPING_VERSION


def test_the_settlement_event_legs_match_the_decomposition():
    """The event's legs are the decomposition's legs, in the event envelope."""
    event = build_settlement_event(_PAYMENT, _TXN, _CLEARING_ACCOUNT, _COA)
    assert event["debitLeg"]["glAccountCode"] == "1131"
    assert event["creditLeg"]["glAccountCode"] == "1111"
    assert event["debitLeg"]["amount"] == event["creditLeg"]["amount"]
    assert event["eventType"] == EVENT_PAYMENT_SETTLEMENT
    assert event["postingStatus"] == "PENDING"
    assert event["meta"]["subLedgerType"] == "CLEARING_AND_SETTLEMENT"


def test_the_settlement_event_uses_the_clearing_amount_not_the_fx_diverged_amount():
    """FR-7.6 hazard — for a cross-border FX wire, `payment.amount` is diverged by `_plan_fx`
    to the settlement-currency amount, while `txn.amount` is the clearing amount credited to
    1131 at stage 6. The settlement event must debit/credit 1131 by `txn.amount` so the
    clearing account nets to zero — NOT by the diverged `payment.amount` (which would leave
    1131 non-zero and break stage-8 leg 3)."""
    payment = {**_PAYMENT, "amount": 1080.0, "currency": "USD", "fxRate": 1.08,
               "instructedAmount": 1000.0, "instructedCurrency": "EUR"}
    txn = {**_TXN, "amount": 1000.0, "currency": "EUR"}
    event = build_settlement_event(payment, txn, _CLEARING_ACCOUNT, _COA)
    # Legs carry the clearing amount (1000 EUR = 100000 minor), not the diverged 1080 USD.
    assert event["debitLeg"]["amount"] == 100_000
    assert event["creditLeg"]["amount"] == 100_000
    assert event["debitLeg"]["currency"] == "EUR"
    assert event["creditLeg"]["currency"] == "EUR"
