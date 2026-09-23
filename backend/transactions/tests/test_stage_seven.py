"""Stage 7 — clearing & settlement. Doc 21 §4.

Four kinds of test, per the playbook's Phase H:

* **behaviour** — one per R-row that changes runtime behaviour, driven through the saga
* **the four outcomes** — B4's matched/delayed/unmatched/exception, each driven through the
  real route, not monkeypatched (defect 2026-09-01 `unreachable-control`)
* **the settlement model** — B5's correspondent/central-bank selection
* **regression** — the internal transfer still settles in stage 5's ACID block; settle is a no-op

Spec conformance for `settlementPositions` lives in the conformance suite once Q51 ratifies
the shape. The `settlementStatus` enum values are asserted here against the spec's array.
"""

import pytest

from tests.test_payments_service import (  # reuse the fixtures, don't fork them
    DEBTOR,
    _initiate,
    _initiate_external,
    CREDITOR,
    db,          # noqa: F401 - pytest fixture
    service,     # noqa: F401 - pytest fixture
)


def _payment(db):
    pays = getattr(db["payments"], "docs", None) or db["payments"].inserted
    return pays[0]


def _settlement_positions(db):
    return getattr(db.get("settlementPositions"), "docs", None) or []


# --- R1/R15: internal transfer settles in stage 5, settle is a no-op -----------

def test_an_internal_transfer_is_already_settled_when_settle_runs(service, db):
    """B3 — for a book transfer, settlement IS the money move. `settle.run` is a no-op."""
    payment = _initiate(service)  # INTERNAL rail
    assert payment["status"] == "SETTLED"
    # settlementStatus is never written for an internal transfer — the key may be absent.
    assert _payment(db)["lifecycle"].get("settlementStatus") is None, (
        "settle.run must not stamp settlementStatus on an internal transfer — "
        "it was never external, so the axis is unused"
    )
    assert _settlement_positions(db) == [], "no settlementPositions for an internal transfer"


# --- B4: her four outcomes, each driven through the real saga ------------------

def test_matched_outcome_settles_the_payment(service, db):
    """B4 matched → settlementStatus SETTLED, currentState SETTLED, settlementPositions written."""
    _initiate_external(service, settlement_outcome="MATCHED")

    payment = _payment(db)
    assert payment["lifecycle"]["currentState"] == "SETTLED"
    assert payment["lifecycle"]["settlementStatus"] == "SETTLED"
    assert payment["clearing"]["settledAt"] is not None
    assert payment["clearing"]["batchRef"] is not None

    positions = _settlement_positions(db)
    assert len(positions) == 1
    assert positions[0]["outcome"] == "MATCHED"
    assert positions[0]["settlementStatus"] == "SETTLED"
    assert positions[0]["simulated"] is True


def test_delayed_outcome_holds_at_in_progress(service, db):
    """B4 delayed → settlementStatus PENDING, currentState stays IN_PROGRESS, saga halts."""
    _initiate_external(service, settlement_outcome="DELAYED")

    payment = _payment(db)
    assert payment["lifecycle"]["currentState"] == "IN_PROGRESS"
    assert payment["lifecycle"]["settlementStatus"] == "PENDING"
    assert payment["clearing"]["settledAt"] is None
    assert payment["clearing"]["settlementDate"] is not None, "delayed has a future value date"

    positions = _settlement_positions(db)
    assert len(positions) == 1, "FR-7.4: a position is written for every outcome, including delayed"
    assert positions[0]["outcome"] == "DELAYED"
    assert positions[0]["actualAmount"] is None, "delayed: actual settlement not yet known"
    assert positions[0]["expectedAmount"] is not None


def test_unmatched_outcome_fails_the_payment(service, db):
    """B4 unmatched → settlementStatus FAILED, currentState FAILED, saga halts.

    FR-7.3 / Doina (Sep 17): UNMATCHED stamps a specific discrepancy amount + reason on
    `clearing`, ready for the (deferred) Stage 9 exception queue — not just a FAILED label.
    """
    _initiate_external(service, settlement_outcome="UNMATCHED")

    payment = _payment(db)
    assert payment["lifecycle"]["currentState"] == "FAILED"
    assert payment["lifecycle"]["settlementStatus"] == "FAILED"
    assert payment["clearing"]["rejectionCode"] is not None
    # The discrepancy amount = expected (the clearing amount) minus actual (0 settled).
    assert payment["clearing"]["discrepancyAmount"] == payment["amount"]
    assert payment["clearing"]["discrepancyReason"] == payment["clearing"]["rejectionCode"]

    positions = _settlement_positions(db)
    assert len(positions) == 1, "FR-7.4: a position is written even for a rejected settlement"
    assert positions[0]["outcome"] == "UNMATCHED"
    assert positions[0]["actualAmount"] == 0, "unmatched: nothing settled"


def test_exception_outcome_returns_the_payment(service, db):
    """B4 exception → settlementStatus RETURNED, currentState RETURNED, saga halts."""
    _initiate_external(service, settlement_outcome="EXCEPTION")

    payment = _payment(db)
    assert payment["lifecycle"]["currentState"] == "RETURNED"
    assert payment["lifecycle"]["settlementStatus"] == "RETURNED"
    assert payment["clearing"]["returnCode"] is not None

    positions = _settlement_positions(db)
    assert len(positions) == 1, "FR-7.4: a position is written even for a returned settlement"
    assert positions[0]["outcome"] == "EXCEPTION"
    assert positions[0]["actualAmount"] == 0, "exception: nothing settled"


# --- B4: every settlementStatus value is from the spec enum -------------------

_SPEC_SETTLEMENT_STATUS_ENUM = {"PENDING", "SETTLED", "FAILED", "RETURNED", None}


@pytest.mark.parametrize("outcome,expected_status", [
    ("MATCHED", "SETTLED"),
    ("DELAYED", "PENDING"),
    ("UNMATCHED", "FAILED"),
    ("EXCEPTION", "RETURNED"),
])
def test_every_settlement_status_is_in_the_spec_enum(service, db, outcome, expected_status):
    """The 2026-04-28 enum-drift rule: every value sourced from the spec's enum array."""
    _initiate_external(service, settlement_outcome=outcome)
    status = _payment(db)["lifecycle"]["settlementStatus"]
    assert status in _SPEC_SETTLEMENT_STATUS_ENUM
    assert status == expected_status


# --- FR-7.3: the outcome is an enum, not a free string (Doina Sep 17) -----------

def test_an_unknown_settlement_outcome_is_rejected_at_the_contract():
    """Doina: "defined as string in the db - shouldn't it be as enums". The request boundary
    admits only MATCHED/UNMATCHED/DELAYED/EXCEPTION — a free string 422's here, instead of
    reaching `_OUTCOME_TO_STATUS` and raising KeyError → HTTP 500 as it did before.
    """
    from pydantic import ValidationError

    from api_models import PaymentSettlementInitiateRequest

    for bad in ("bogus", "matched", "Settled", ""):
        with pytest.raises(ValidationError):
            PaymentSettlementInitiateRequest(paymentId="PAY-1", outcome=bad)

    # The four legal values construct cleanly.
    for ok in ("MATCHED", "UNMATCHED", "DELAYED", "EXCEPTION", None):
        PaymentSettlementInitiateRequest(paymentId="PAY-1", outcome=ok)


def test_the_initiate_request_accepts_the_simulation_outcome():
    """The wizard control threads through `simulatedSettlementOutcome` on the Initiate
    contract (default None → MATCHED happy path)."""
    from api_models import PaymentOrderInitiateRequest

    # A minimal valid external-wire request carrying the simulation lever.
    req = PaymentOrderInitiateRequest(
        customerId="CUST-1", type="CREDIT_TRANSFER", rail="WIRE",
        debtor={"accountId": "ACC-1"},
        creditor={"accountNo": "9999", "name": "Acme", "bic": "BARCGB22"},
        instructedAmount=100.0, instructedCurrency="USD",
        simulatedSettlementOutcome="UNMATCHED",
    )
    assert req.simulatedSettlementOutcome == "UNMATCHED"
    # Default is None — the saga treats that as MATCHED.
    plain = PaymentOrderInitiateRequest(
        customerId="CUST-1", type="CREDIT_TRANSFER", rail="WIRE",
        debtor={"accountId": "ACC-1"},
        creditor={"accountNo": "9999", "name": "Acme", "bic": "BARCGB22"},
        instructedAmount=100.0, instructedCurrency="USD",
    )
    assert plain.simulatedSettlementOutcome is None


# --- B5: settlement model selection -------------------------------------------

def test_a_correspondent_wire_settles_through_nostro(service, db):
    """B5 model 1 — a wire that required a correspondent settles via Cr 1111 Nostro.

    The routing module resolves a correspondent BIC from `_CORRESPONDENT_BY_COUNTRY` when
    `network == SWIFT`. The fixture uses `bankCountry: "GB"` which maps to `BARCGB22`.
    """
    _initiate(
        service,
        creditor_account_ref=None,
        creditor_party={"accountNo": "9876543210", "name": "Acme Corp",
                        "bic": "BARCGB22", "bankName": "Barclays", "bankCountry": "GB"},
        payment_rail="WIRE",
        wire_details={
            "wireType": "INTERNATIONAL",
            "messageDefinitionIdentifier": "pain.001.001.09",
            "network": "SWIFT",
        },
    )
    positions = _settlement_positions(db)
    assert len(positions) == 1
    assert positions[0]["model"] == "CORRESPONDENT"
    assert positions[0]["settlementAccountCode"] == "1111"


def test_a_domestic_wire_settles_through_central_bank(service, db):
    """B5 model 2 — a domestic wire with no correspondent settles via Cr 1121 Reserves."""
    _initiate_external(
        service,
        wire_details={
            "wireType": "DOMESTIC",
            "messageDefinitionIdentifier": "pain.001.001.09",
            "network": "FEDWIRE",
        },
    )
    positions = _settlement_positions(db)
    assert len(positions) == 1
    assert positions[0]["model"] == "CENTRAL_BANK"
    assert positions[0]["settlementAccountCode"] == "1121"


# --- R12: settlementPositions shape ------------------------------------------

def test_settlement_positions_carries_the_payment_id_both_ways(service, db):
    """R12 — the settlementPosition is traceable to the payment and vice versa."""
    _initiate_external(service)
    payment = _payment(db)
    positions = _settlement_positions(db)
    assert positions[0]["paymentId"] == payment["paymentId"]


# --- FR-7.4 / FR-7.6: position shape (expected/actual + FX) --------------------

def test_a_cross_border_fx_wire_records_the_fx_exchange_on_the_position(service, db):
    """FR-7.6 — a cross-border FX wire records the correspondent/nostro FX exchange
    (fxRate, instructed amount/currency, settlement amount/currency) on the
    settlementPositions doc. FR-7.4: expectedAmount is the clearing amount (read from the
    transactions doc), distinct from grossAmount (the FX-diverged settlement amount)."""
    _initiate_external(service, instructed_currency="EUR", instructed_amount=1000.0)

    payment = _payment(db)
    assert payment["fxRate"] is not None, "FX applied — debtor USD, instructed EUR"

    positions = _settlement_positions(db)
    assert len(positions) == 1
    pos = positions[0]

    # FR-7.6: the FX exchange is recorded on the position.
    assert pos["fxRate"] == payment["fxRate"]
    assert pos["instructedAmount"] == 1000.0
    assert pos["instructedCurrency"] == "EUR"
    assert pos["settlementAmount"] == payment["amount"]
    assert pos["settlementCurrency"] == payment["currency"]

    # FR-7.4: expected (clearing amount from the transactions doc) vs gross (settlement
    # amount) are distinct on an FX wire.
    txn = db["transactions"].find_one({"paymentId": payment["paymentId"]})
    assert pos["expectedAmount"] == txn["amount"]
    assert pos["grossAmount"] == payment["amount"]
    assert pos["expectedAmount"] != pos["grossAmount"], (
        "FX wire: the clearing amount (txn) must differ from the settlement amount (payment)"
    )
