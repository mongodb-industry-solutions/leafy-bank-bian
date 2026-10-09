"""Stage 3 funds reservation: hold on validation, release at posting or on rejection."""

from types import SimpleNamespace

import pytest

from contexts.payment_order_initiation.application import funds_reservation as fr
from process import payment_lifecycle
from tests.test_payments_service import (  # noqa: F401  (fixtures)
    CREDITOR,
    DEBTOR,
    FakeCollection,
    _initiate,
    db,
    service,
)


def _ctx(db, payment_id, amount):
    collections = SimpleNamespace(db=db, accounts=db["accounts"])
    return SimpleNamespace(
        collections=collections, payment_id=payment_id, instructed_amount=amount,
        debtor_account_ref=DEBTOR, debtor_account={"currency": "USD"},
    )


def _balance(db, account_id=DEBTOR):
    return db["accounts"].find_one({"accountId": account_id})["balance"]


def test_reserve_reduces_available_and_moves_it_to_hold(db):
    reservation = fr.reserve(_ctx(db, "PAY-aaaa1111", 4_000.0))

    assert reservation["reservationId"] == "RSV-aaaa1111"
    assert reservation["status"] == fr.ACTIVE
    balance = _balance(db)
    assert (balance["available"], balance["hold"]) == (6_000.0, 4_000.0)
    # The ledger still shows the money as the customer's until the debit.
    assert (balance["current"], balance["ledger"]) == (10_000.0, 10_000.0)


def test_reserve_is_idempotent_per_payment(db):
    ctx = _ctx(db, "PAY-aaaa1111", 4_000.0)
    first = fr.reserve(ctx)
    second = fr.reserve(ctx)

    assert second["reservationId"] == first["reservationId"]
    assert _balance(db)["available"] == 6_000.0
    assert len(db[fr.COLLECTION].docs) == 1


def test_second_payment_cannot_reserve_the_same_money(db):
    assert fr.reserve(_ctx(db, "PAY-aaaa1111", 6_000.0)) is not None

    assert fr.reserve(_ctx(db, "PAY-bbbb2222", 5_000.0)) is None
    assert _balance(db)["available"] == 4_000.0
    assert len(db[fr.COLLECTION].docs) == 1


def test_release_restores_the_balance_once(db):
    fr.reserve(_ctx(db, "PAY-aaaa1111", 4_000.0))

    assert fr.release(db, "PAY-aaaa1111", reason="test")["status"] == fr.RELEASED
    assert fr.release(db, "PAY-aaaa1111", reason="again") is None
    balance = _balance(db)
    assert (balance["available"], balance["hold"]) == (10_000.0, 0)


def test_a_settled_payment_releases_then_debits_once(service, db):
    payment = _initiate(service, instructed_amount=250.0)

    reservation = db[fr.COLLECTION].docs[0]
    assert reservation["status"] == fr.RELEASED
    assert reservation["releaseReason"].startswith("Released at posting")
    balance = _balance(db)
    assert (balance["available"], balance["current"], balance["ledger"]) == (9_750.0,) * 3
    assert balance["hold"] == 0
    assert payment["refs"]["fundsReservationId"] == reservation["reservationId"]
    funds = next(c for c in payment["checks"] if c["name"] == "funds_available")
    assert reservation["reservationId"] in funds["detail"]


def test_insufficient_funds_reserves_nothing(service, db):
    with pytest.raises(ValueError, match="Insufficient available balance"):
        _initiate(service, instructed_amount=10_000.01)

    assert db[fr.COLLECTION].docs == []
    balance = _balance(db)
    assert (balance["available"], balance.get("hold", 0)) == (10_000.0, 0)


def test_rejection_after_stage_three_releases_the_hold(service, db, monkeypatch):
    def refuse(_ctx):
        raise ValueError("routing refused")

    monkeypatch.setattr(payment_lifecycle, "STAGES", [
        (label, refuse if label == "4 orchestrate" else stage)
        for label, stage in payment_lifecycle.STAGES
    ])

    with pytest.raises(ValueError, match="routing refused"):
        _initiate(service, instructed_amount=250.0)

    assert db[fr.COLLECTION].docs[0]["status"] == fr.RELEASED
    balance = _balance(db)
    assert (balance["available"], balance["current"], balance.get("hold", 0)) == (10_000.0, 10_000.0, 0)
