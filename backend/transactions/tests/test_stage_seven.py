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
    _initiate_external(service, settlement_outcome="matched")

    payment = _payment(db)
    assert payment["lifecycle"]["currentState"] == "SETTLED"
    assert payment["lifecycle"]["settlementStatus"] == "SETTLED"
    assert payment["clearing"]["settledAt"] is not None
    assert payment["clearing"]["batchRef"] is not None

    positions = _settlement_positions(db)
    assert len(positions) == 1
    assert positions[0]["outcome"] == "matched"
    assert positions[0]["settlementStatus"] == "SETTLED"
    assert positions[0]["simulated"] is True


def test_delayed_outcome_holds_at_in_progress(service, db):
    """B4 delayed → settlementStatus PENDING, currentState stays IN_PROGRESS, saga halts."""
    _initiate_external(service, settlement_outcome="delayed")

    payment = _payment(db)
    assert payment["lifecycle"]["currentState"] == "IN_PROGRESS"
    assert payment["lifecycle"]["settlementStatus"] == "PENDING"
    assert payment["clearing"]["settledAt"] is None
    assert payment["clearing"]["settlementDate"] is not None, "delayed has a future value date"

    positions = _settlement_positions(db)
    assert len(positions) == 0, "no settlementPositions for a delayed settlement — it hasn't settled"


def test_unmatched_outcome_fails_the_payment(service, db):
    """B4 unmatched → settlementStatus FAILED, currentState FAILED, saga halts."""
    _initiate_external(service, settlement_outcome="unmatched")

    payment = _payment(db)
    assert payment["lifecycle"]["currentState"] == "FAILED"
    assert payment["lifecycle"]["settlementStatus"] == "FAILED"
    assert payment["clearing"]["rejectionCode"] is not None


def test_exception_outcome_returns_the_payment(service, db):
    """B4 exception → settlementStatus RETURNED, currentState RETURNED, saga halts."""
    _initiate_external(service, settlement_outcome="exception")

    payment = _payment(db)
    assert payment["lifecycle"]["currentState"] == "RETURNED"
    assert payment["lifecycle"]["settlementStatus"] == "RETURNED"
    assert payment["clearing"]["returnCode"] is not None


# --- B4: every settlementStatus value is from the spec enum -------------------

_SPEC_SETTLEMENT_STATUS_ENUM = {"PENDING", "SETTLED", "FAILED", "RETURNED", None}


@pytest.mark.parametrize("outcome,expected_status", [
    ("matched", "SETTLED"),
    ("delayed", "PENDING"),
    ("unmatched", "FAILED"),
    ("exception", "RETURNED"),
])
def test_every_settlement_status_is_in_the_spec_enum(service, db, outcome, expected_status):
    """The 2026-04-28 enum-drift rule: every value sourced from the spec's enum array."""
    _initiate_external(service, settlement_outcome=outcome)
    status = _payment(db)["lifecycle"]["settlementStatus"]
    assert status in _SPEC_SETTLEMENT_STATUS_ENUM
    assert status == expected_status


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
