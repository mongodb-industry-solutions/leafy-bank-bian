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
    assert credit.gl_account_code == "1111"  # Nostro/Central Bank Cash
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


# --- the incoming wire: the mirror settlement event (FR-7.IN1) ----------------
#
# Found live on 2026-09-29: an inbound payment settled on arrival raised in
# `build_settlement_event` ("no clearing.settlementAccountCode") and crash-looped the worker,
# blocking every settlement queued behind it — the 2026-07-01 incident class, reproduced by
# the incoming flow. These pin both halves of the fix: the stamped routing field, and the
# mirrored legs + the payer-side clearing-account resolution.

def test_an_inbound_settlement_builds_the_mirror_legs():
    """FR-7.IN1 — `Dr Nostro / Cr Wire Clearing`, the mirror of outgoing FR-7.1.

    Asserted on the built event's legs, not just the decomposition, so the direction branch
    inside `build_settlement_event` itself is what is pinned."""
    payment = {**_PAYMENT, "direction": "INBOUND"}
    event = build_settlement_event(payment, _TXN, _CLEARING_ACCOUNT, _COA)

    assert event["debitLeg"]["glAccountCode"] == "1111", "the nostro must be DEBITED inbound"
    assert event["creditLeg"]["glAccountCode"] == "1131", "the clearing hold must be RELEASED"
    # Stage 7 legs and control roll-ups are exactly Dr 1111 (1110) / Cr 1131 (1130).
    assert event["debitLeg"]["controlAccountCode"] == "1110"
    assert event["creditLeg"]["controlAccountCode"] == "1130"
    assert event["debitLeg"]["amount"] == event["creditLeg"]["amount"]
    assert event["idempotencyKey"] == "PAY-test0001-SETTLEMENT", (
        "the key is per-payment, not per-direction — one payment, one settlement event"
    )
    assert "Inbound settlement posting" in event["description"]


def test_an_outbound_settlement_keeps_its_original_legs():
    """No `direction` field on the payment = outbound, the pre-inbound behaviour. Pinning
    that the mirror did not silently flip the outbound event too."""
    event = build_settlement_event(_PAYMENT, _TXN, _CLEARING_ACCOUNT, _COA)

    assert event["debitLeg"]["glAccountCode"] == "1131"
    assert event["creditLeg"]["glAccountCode"] == "1111"


def test_the_clearing_account_is_resolved_from_the_payer_side_for_inbound():
    """`process_settlement` picks the clearing account off the transactions doc by
    DIRECTION: outbound payee = clearing, inbound payer = clearing. Reading `payee`
    unconditionally resolves the CUSTOMER on an inbound payment, whose gl.accountCode is a
    deposit control — the settlement would debit the customer's deposit.

    Exercised through a fake connection, so the resolution (not just the pure builder) is
    what is pinned."""
    from workers import settlement_worker

    customer_txn = {
        **_TXN,
        # The inbound shape: payer = clearing, payee = the customer.
        "payer": {"accountId": "ACC-CLEARING-WIRE"},
        "payee": {"accountId": "ACC-customer01"},
    }
    payment = {**_PAYMENT, "direction": "INBOUND"}

    class _Coll(dict):
        def __init__(self, docs):
            super().__init__()
            self.docs = docs

        def find_one(self, flt, *a, **kw):
            for d in self.docs:
                if all(d.get(k) == v for k, v in flt.items()):
                    return d
            return None

    class _Db(dict):
        def __init__(self):
            super().__init__()
            self["transactions"] = _Coll([customer_txn])
            self["accounts"] = _Coll([
                _CLEARING_ACCOUNT,
                {"accountId": "ACC-customer01", "type": "CHECKING",
                 "gl": {"accountCode": "2111"}},
            ])
            self["ledgerEvents"] = _Coll([])

    class _Conn:
        def __init__(self, db):
            self._db = db

        def get_collection(self, _name, coll):
            return self._db[coll]

    inserted = []

    class _Capture(_Coll):
        """A collection that records inserts instead of writing."""

        def insert_one(self, doc):
            self.docs.append(doc)
            inserted.append(doc)

    conn = _Conn(_Db())
    conn._db["ledgerEvents"] = _Capture([])

    settlement_worker.process_settlement(payment, conn, "test-db", _COA)

    assert len(inserted) == 1
    legs = {inserted[0]["debitLeg"]["glAccountCode"],
            inserted[0]["creditLeg"]["glAccountCode"]}
    assert legs == {"1111", "1131"}, (
        "the clearing account came off the PAYER side — the customer's deposit (2111) "
        "must not appear in an inbound settlement event"
    )


# --- stage 9: the approved short-pay correction (chargeBearer DEBT) -------------

class _StubCoAWith5214(_StubCoA):
    _CONTROLS = {**_StubCoA._CONTROLS, "5214": "5210"}


def test_an_approved_adjustment_posts_dr_5214_cr_nostro():
    from shared.posting_rules import EVENT_SETTLEMENT_ADJUSTMENT
    from workers.settlement_worker import build_adjustment_event

    payment = {**_PAYMENT, "clearing": {
        **_PAYMENT["clearing"],
        "settlementAdjustment": {"amount": 25.0, "currency": "USD",
                                 "chargeBearer": "DEBT", "exceptionId": "EXC-1"},
    }}
    event = build_adjustment_event(payment, _CLEARING_ACCOUNT, _StubCoAWith5214())
    assert event["eventType"] == EVENT_SETTLEMENT_ADJUSTMENT
    assert event["idempotencyKey"] == "PAY-test0001-ADJ"
    assert event["debitLeg"]["glAccountCode"] == "5214"
    assert event["creditLeg"]["glAccountCode"] == "1111"
    assert event["debitLeg"]["amount"] == event["creditLeg"]["amount"] == 2500


def test_no_adjustment_event_without_an_approved_adjustment():
    from workers.settlement_worker import build_adjustment_event
    assert build_adjustment_event(_PAYMENT, _CLEARING_ACCOUNT, _StubCoAWith5214()) is None


def test_a_chart_without_5214_degrades_instead_of_crashing_the_worker():
    from workers.settlement_worker import build_adjustment_event
    payment = {**_PAYMENT, "clearing": {
        **_PAYMENT["clearing"],
        "settlementAdjustment": {"amount": 25.0, "currency": "USD"},
    }}
    assert build_adjustment_event(payment, _CLEARING_ACCOUNT, _COA) is None


def test_the_seed_names_gl_1111_nostro_central_bank_cash():
    """The rename must reach the seed, or a fresh database shows the old name."""
    import json
    from pathlib import Path

    seed = Path(__file__).resolve().parents[2] / "data" / "sample" / "leafy_bank_bian.glAccounts.json"
    by_code = {a["accountCode"]: a for a in json.loads(seed.read_text())}
    assert by_code["1111"]["accountName"] == "Nostro/Central Bank Cash"
    assert by_code["1121"]["accountName"] == "Minimum Reserve Requirements"
