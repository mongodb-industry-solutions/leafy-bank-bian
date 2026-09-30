"""Reconciliation plan A1 — the correspondent's camt.053 statement generator.

Wires are driven through the real saga (`_initiate_external` + `settle.complete_due`), never
hand-built positions — the fixture-fidelity rule. `now` is passed explicitly so windows are
deterministic; `complete_due` stamps the real clock, so "later" means `now + minutes`.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from contexts.financial_gateway.application.statement import generate_statement
from contexts.financial_gateway.domain import camt053
from contexts.payment_settlement import settle
from tests.test_payments_service import (  # reuse the fixtures, don't fork them
    FakeConnection,
    _initiate,
    _initiate_external,
    db,
    service,
)


# The fixture's external creditor is a US bank with no correspondent, so `settle` routes it
# to the central-bank account (1121, Fedwire), not the correspondent nostro (1111).
SETTLEMENT_ACCOUNT = "1121"


def _later(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _settled_wire(service, db, outcome=None, amount=250.0) -> str:
    payment = _initiate_external(service, instructed_amount=amount,
                                 statement_outcome=outcome)
    settle.complete_due(FakeConnection(db), "leafy_bank_bian", delay_seconds=0)
    return payment["paymentId"]


def _line(doc, payment_id):
    return next(e for e in doc["entries"] if e.get("simulatedPaymentId") == payment_id)


# --- pure builder -------------------------------------------------------------

def test_the_reference_is_rekeyed_the_way_a_correspondent_does_it():
    assert camt053.altered_reference("PAY-8ebaa746") == "8EBAA746/LEAFYBK"
    assert len(camt053.altered_reference("PAY-0123456789abcdef")) == 16


@pytest.mark.parametrize("amount,expected", [
    (1250.0, 1205.0),     # lowest differing adjacent pair swapped
    (250.0, 205.0),
    (1111.0, 1111.0),     # no differing pair: degrades to clean
    (5.0, 5.0),
])
def test_transposition_swaps_adjacent_digits_or_degrades_to_clean(amount, expected):
    assert camt053.transposed_amount(amount) == expected


def test_the_xml_carries_only_iso_elements():
    """Element-name guard (defect 2026-09-02): our annotations must never leak into the
    correspondent's message."""
    entry = camt053.entry_for({"paymentId": "PAY-1", "grossAmount": 100.0, "currency": "USD"},
                              camt053.FEE_DEDUCTED)
    now = datetime.now(timezone.utc)
    message = camt053.build(statement_id="STMT-1", account_code="1111", currency="USD",
                            window_from=now, window_to=now, sequence=1, opening_balance=0.0,
                            entries=[entry], now=now)
    xml = camt053.to_xml(message)
    assert "<BkToCstmrStmt>" in xml and camt053.NAMESPACE in xml
    for leaked in ("recon", "simulated", "entries", "creditDebit"):
        assert leaked not in xml
    stmt = camt053.body(message)["Stmt"][0]
    assert isinstance(stmt["Ntry"], list) and isinstance(stmt["Bal"], list)


# --- the generator, over the real saga ---------------------------------------

def test_each_lever_produces_its_line(service, db):
    clean = _settled_wire(service, db)
    fee = _settled_wire(service, db, camt053.FEE_DEDUCTED)
    ref = _settled_wire(service, db, camt053.REFERENCE_ALTERED)
    swap = _settled_wire(service, db, camt053.AMOUNT_TRANSPOSED)

    doc = generate_statement(db, account_code=SETTLEMENT_ACCOUNT, include_orphan=False, now=_later(1))

    assert _line(doc, clean)["amount"] == 250.0 and _line(doc, clean)["reference"] == clean
    assert _line(doc, fee)["amount"] == 225.0 and _line(doc, fee)["charges"] == 25.0
    assert _line(doc, ref)["reference"] == camt053.altered_reference(ref)
    assert _line(doc, swap)["amount"] == 205.0
    assert all(e["recon"]["status"] == camt053.RECON_UNMATCHED for e in doc["entries"])
    assert doc["purpose"] == "ACCOUNT_STATEMENT" and doc["paymentId"] is None
    assert doc["statement"]["closingBalance"] == -(250 + 225 + 250 + 205)


def test_a_late_line_is_held_one_statement_and_booked_once(service, db):
    late = _settled_wire(service, db, camt053.LATE)
    first = generate_statement(db, account_code=SETTLEMENT_ACCOUNT, include_orphan=False, now=_later(1))
    assert all(e.get("simulatedPaymentId") != late for e in first["entries"])

    second = generate_statement(db, account_code=SETTLEMENT_ACCOUNT, include_orphan=False, now=_later(2))
    assert _line(second, late)["amount"] == 250.0, "LATE arrives clean, one statement later"
    assert generate_statement(db, account_code=SETTLEMENT_ACCOUNT, include_orphan=False, now=_later(3)) is None


def test_generation_is_idempotent_and_statements_chain(service, db):
    _settled_wire(service, db)
    first = generate_statement(db, account_code=SETTLEMENT_ACCOUNT, include_orphan=False, now=_later(1))
    assert generate_statement(db, account_code=SETTLEMENT_ACCOUNT, include_orphan=False, now=_later(2)) is None

    _settled_wire(service, db)
    second = generate_statement(db, account_code=SETTLEMENT_ACCOUNT, include_orphan=False, now=_later(3))
    assert second["statement"]["sequence"] == 2
    assert second["statement"]["window"]["from"] == first["statement"]["window"]["to"]
    assert second["statement"]["openingBalance"] == first["statement"]["closingBalance"]


def test_an_orphan_has_no_payment_behind_it(service, db):
    _settled_wire(service, db)
    doc = generate_statement(db, account_code=SETTLEMENT_ACCOUNT, now=_later(1), rng=random.Random(7))
    orphans = [e for e in doc["entries"] if e["simulatedPaymentId"] is None]
    assert len(orphans) == 1
    assert orphans[0]["reference"].startswith(camt053.ORPHAN_REF_PREFIX)
    assert db["payments"].find_one({"paymentId": orphans[0]["reference"]}) is None


def test_unsettled_and_internal_payments_are_not_booked(service, db):
    _initiate(service)                                  # internal: never on the nostro
    _initiate_external(service)                         # captured, not yet settled
    assert generate_statement(db, account_code=SETTLEMENT_ACCOUNT, now=_later(1)) is None


def test_the_lever_is_persisted_for_hold_and_resume(service, db):
    pid = _settled_wire(service, db, camt053.FEE_DEDUCTED)
    assert db["payments"].find_one({"paymentId": pid})["simulatedStatementOutcome"] == "FEE_DEDUCTED"


def test_an_unknown_statement_outcome_is_rejected_at_the_contract():
    from api_models import PaymentOrderInitiateRequest
    with pytest.raises(ValidationError):
        PaymentOrderInitiateRequest.model_validate({"simulatedStatementOutcome": "BOGUS"})
