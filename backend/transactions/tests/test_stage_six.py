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

def test_an_on_us_wire_carries_the_fee_across_the_boundary(service, db):  # noqa: F811
    """⚠️ THE reachability test for the whole fee rule (B3(d)).

    Of the three payment shapes, only this one exercises a fee leg:
      * internal transfer — stage 3 levies a charge on `rail == "WIRE"` only, so no fee
      * external wire     — has a fee, but `execute.py:351` halts it before any
                            `transactions` doc exists, so the ledger never sees the payment
      * on-us wire        — `rail: WIRE` with a creditor account on a *different* Leafy Bank
                            customer. Not external (`capture.py:76`), not own-account
                            (`:96` — "same customer, NOT same bank"), so it settles AND
                            carries the charge.

    If this test ever stops passing, the fee posting rule in the ledger is dead code and
    doc 20 B3 has to be re-decided, not patched.
    """
    _initiate(service, payment_rail="WIRE")
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
    settles via `settle.py` (default outcome: matched) — the boundary document exists, so
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
    _initiate(service, payment_rail="WIRE", instructed_amount=250.0)
    txn = _txn(db)
    assert txn["amount"] == 250.0
    assert txn["baseAmount"] == 250.0
    assert _payment(db)["amount"] == 250.0


def test_the_transaction_doc_gained_only_the_two_fee_fields(service, db):  # noqa: F811
    """§7 — stage 6's single boundary change, additive only. Every field the ledger's
    `ingest_worker` reads (doc 12 §1) is still present and unrenamed."""
    _initiate(service, payment_rail="WIRE")
    txn = _txn(db)
    for field in ("amount", "paymentId", "currency", "paymentType", "rail", "sourceSystem"):
        assert field in txn
    assert txn["payer"]["accountId"] and txn["payee"]["accountId"]
    assert "feeAmount" in txn and "feeCurrency" in txn
    # Stage 5's boundary field is untouched by stage 6.
    assert "paymentExecutionId" in txn


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
