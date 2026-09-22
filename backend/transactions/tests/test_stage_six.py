"""Stage 6 — the transactions-service half: the fee's boundary field. Doc 20 step 5, B3(a).

Everything else in stage 6 lives in the ledger service; only the boundary document is written
here. These tests exist to hold two lines that matter more than they look:

* the fee crosses the boundary **additively** — `amount` is the settlement amount and the
  ledger's primary input (`enrichment_plan.py:264`), so a fee is a new leg, never a deduction
* the fee is **reachable**, on exactly one payment shape (B3(d)), and this file names it
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests.test_payments_service import (  # reuse the fixtures, don't fork them
    _initiate,
    _initiate_external,
    db,          # noqa: F401 - pytest fixture
    service,     # noqa: F401 - pytest fixture
)


def _txn(db):
    assert len(db["transactions"].docs) == 1
    return db["transactions"].docs[0]


def _payment(db):
    pays = getattr(db["payments"], "docs", None) or db["payments"].inserted
    return pays[0]


# --- the fee crosses the boundary (step 5) -----------------------------------

def test_an_external_wire_carries_the_fee_across_the_boundary(service, db):  # noqa: F811
    """⚠️ THE reachability test for the whole fee rule (B3(d)).

    Of the three payment shapes, only a wire exercises a fee leg:
      * internal transfer — stage 3 levies a charge on `rail == "WIRE"` only, so no fee
      * external wire      — `rail: WIRE`, so stage 3 levies the charge, AND stage 7 (landed
                             2026-09-03) makes it write a `transactions` doc (payee = the
                             clearing account) and settle via `settle.py`. So the boundary
                             document exists and the fee reaches the ledger.

    The on-us wire (`WIRE` to a held creditor) was once the *only* shape that both settled
    and carried a fee, because the pre-stage-7 halt kept an external wire from writing a
    `transactions` doc. That halt is gone and the on-us wire is now disallowed at the
    contract (`api_models.py` — defect 2026-09-08 `discriminator-conflation`), so the
    external wire is the fee path. If this test ever stops passing, the fee posting rule in
    the ledger is dead code and doc 20 B3 has to be re-decided, not patched.

    Reads from the DB, not the value returned by `initiate_payment`: for an external wire
    `ctx.result` is the stage-5 IN_PROGRESS snapshot, and `settle.run` advances the stored
    document to SETTLED without refreshing `ctx.result` (defect class: the returned payment
    is a stale snapshot once settlement is deferred).
    """
    _initiate_external(service)
    payment = _payment(db)
    assert payment["fees"] == [
        {"type": "WIRE_FEE", "amount": 25.00, "currency": "USD", "chargedTo": "DEBTOR"}
    ]
    assert _txn(db)["feeAmount"] == 25.00
    assert _txn(db)["feeCurrency"] == "USD"


def test_an_internal_transfer_carries_no_fee(service, db):  # noqa: F811
    _initiate(service, payment_rail="INTERNAL")
    assert _payment(db)["fees"] == []
    assert _txn(db)["feeAmount"] == 0.0


def test_an_external_wire_reaches_the_ledger_via_the_clearing_account(service, db):  # noqa: F811
    """Stage 7 doc 21 B1/B3 — the halt is gone. An external wire now writes a `transactions`
    doc (payee = the clearing account), so the ledger's CDC path observes it. The payment
    settles via `settle.py` (default outcome: MATCHED) — the boundary document exists, so
    stage 6's write-back and the GL pipeline are reachable."""
    _initiate_external(service)
    assert len(db["transactions"].docs) == 1
    txn = db["transactions"].docs[0]
    assert txn["payee"]["accountId"] == "ACC-CLEARING-WIRE"
    assert _payment(db)["lifecycle"]["currentState"] == "SETTLED"
    assert _payment(db)["lifecycle"]["settlementStatus"] == "SETTLED"


def test_the_fee_does_not_change_the_settlement_amount(service, db):  # noqa: F811
    """⚠️ The boundary guard. `enrichment_plan.py:264` — *"`amount` must not change. It is the
    settlement amount and the ledger's primary input."* A fee that moved `amount` would be a
    boundary retype disguised as a fee, and the GL would post the wrong principal."""
    _initiate_external(service, instructed_amount=250.0)
    txn = _txn(db)
    assert txn["amount"] == 250.0
    assert txn["baseAmount"] == 250.0
    assert _payment(db)["amount"] == 250.0


def test_the_transaction_doc_gained_only_the_two_fee_fields(service, db):  # noqa: F811
    """§7 — stage 6's single boundary change, additive only. Every field the ledger's
    `ingest_worker` reads (doc 12 §1) is still present and unrenamed."""
    _initiate_external(service)
    txn = _txn(db)
    for field in ("amount", "paymentId", "currency", "paymentType", "rail", "sourceSystem"):
        assert field in txn
    assert txn["payer"]["accountId"] and txn["payee"]["accountId"]
    assert "feeAmount" in txn and "feeCurrency" in txn
    # Stage 5's boundary field is untouched by stage 6.
    assert "paymentExecutionId" in txn


def test_an_on_us_wire_is_rejected_at_the_contract():
    """A WIRE to a creditor held at this bank (an 'on-us wire') is disallowed at the request
    contract (defect 2026-09-08 `discriminator-conflation`) — it would settle atomically inside
    stage 5 and skip clearing & settlement, leaving `settlementStatus`/`settlementPositions`
    unwritten and reconciliation legs 2/3 pending forever. A held-creditor move must use
    rail=INTERNAL; the fee path is an external wire (see
    `test_an_external_wire_carries_the_fee_across...`).

    The rule lives in the Pydantic `PaymentOrderInitiateRequest` validator — the HTTP
    boundary. `PaymentsService.initiate_payment` builds a `PaymentContext` directly and does
    not re-check it, so this test constructs the model the way the router does.
    """
    from api_models import PaymentOrderInitiateRequest

    with pytest.raises(ValueError, match="use rail=INTERNAL"):
        PaymentOrderInitiateRequest(
            customerId="CUST-1",
            type="CREDIT_TRANSFER",
            rail="WIRE",
            debtor={"accountId": "ACC-debtor"},
            creditor={"accountId": "ACC-creditor"},  # held → on-us wire
            instructedAmount=100.0,
            instructedCurrency="USD",
        )


def test_an_external_wire_and_an_internal_transfer_are_accepted(service, db):
    """The on-us wire disallow must not over-fire: an external wire (no accountId) and an
    internal transfer (rail=INTERNAL + accountId) are both legitimate and must still pass the
    contract. Pinning the scope so a future tightening can't silently reject the happy paths."""
    from api_models import PaymentOrderInitiateRequest

    PaymentOrderInitiateRequest(
        customerId="CUST-1", type="CREDIT_TRANSFER", rail="WIRE",
        debtor={"accountId": "ACC-debtor"},
        creditor={"accountNo": "123", "name": "Acme", "bic": "CHASUS33"},
        instructedAmount=100.0, instructedCurrency="USD",
    )
    PaymentOrderInitiateRequest(
        customerId="CUST-1", type="CREDIT_TRANSFER", rail="INTERNAL",
        debtor={"accountId": "ACC-debtor"},
        creditor={"accountId": "ACC-creditor"},
        instructedAmount=100.0, instructedCurrency="USD",
    )


def test_a_creditor_borne_fee_is_not_carried(service, db):  # noqa: F811
    """B3(d) — `_FEE_PAYER_BY_CHARGE_BEARER` can produce a creditor-borne fee from an ISO
    `CRED` charge bearer, and which account such a fee debits is Doina's call (Q44).
    Skipped rather than posted to a guessed account."""
    from contexts.payment_rail.execute import _debtor_borne_fee

    class _Ctx:
        payment_id = "PAY-1"
        payment_doc = {"fees": [
            {"type": "WIRE_FEE", "amount": 25.00, "chargedTo": "CREDITOR"},
        ]}

    assert _debtor_borne_fee(_Ctx()) == 0.0


def test_a_shared_fee_is_carried_for_the_debtor(service, db):  # noqa: F811
    """ISO `SHAR` (shared) means the sending bank charges the debtor its fee — the
    creditor's half is a receivable on the receiving end, not a deduction here. So
    `chargedTo == "SHARED"` posts the full amount to the debtor's ledger, same as DEBTOR."""
    from contexts.payment_rail.execute import _debtor_borne_fee

    class _Ctx:
        payment_id = "PAY-1"
        payment_doc = {"fees": [
            {"type": "WIRE_FEE", "amount": 25.00, "chargedTo": "SHARED"},
        ]}

    assert _debtor_borne_fee(_Ctx()) == 25.00


def test_debtor_borne_fees_are_summed(service, db):  # noqa: F811
    from contexts.payment_rail.execute import _debtor_borne_fee

    class _Ctx:
        payment_id = "PAY-1"
        payment_doc = {"fees": [
            {"type": "WIRE_FEE", "amount": 25.00, "chargedTo": "DEBTOR"},
            {"type": "INTERMEDIARY", "amount": 10.50, "chargedTo": "DEBTOR"},
            {"type": "FX_MARKUP", "amount": 99.00, "chargedTo": "CREDITOR"},
        ]}

    assert _debtor_borne_fee(_Ctx()) == 35.50


def test_no_fee_block_at_all_is_zero_not_an_error(service, db):  # noqa: F811
    from contexts.payment_rail.execute import _debtor_borne_fee

    class _Ctx:
        payment_id = "PAY-1"
        payment_doc = {}

    assert _debtor_borne_fee(_Ctx()) == 0.0


# --- immutability of the money move (AST, not a comment) ---------------------

def test_the_money_move_still_has_exactly_two_balance_updates():
    """The fee is recorded, not deducted — so `_money_move` must still touch exactly two
    account balances. A third would mean a fee started moving money, which is a boundary
    change needing its own plan (`enrichment_plan.py:265`)."""
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "contexts" / "payment_rail" / "execute.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_money_move")
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "find_one_and_update"
    ]
    assert len(calls) == 2, f"expected 2 balance updates in _money_move, found {len(calls)}"
