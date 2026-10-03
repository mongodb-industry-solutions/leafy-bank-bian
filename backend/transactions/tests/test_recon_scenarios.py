"""Plan B3 — the reconciliation scenario pack, driven through the real saga.

The ledger half (matching → exceptions) is covered by ledger tests on the statement shape;
this proves the transactions half produces exactly the facts those tests consume: five
settled wires on the correspondent nostro, each line carrying its lever, plus one orphan.
"""

from __future__ import annotations

from contexts.financial_gateway.application import recon_scenarios
from contexts.financial_gateway.domain import camt053
from tests.test_payments_service import FakeConnection, db, service  # noqa: F401 (fixtures)
from tests.test_statement_camt053 import _later


def _fund(db, amount=50_000.0):
    debtor = db["accounts"].docs[0]
    debtor["balance"].update(current=amount, available=amount, ledger=amount)


def _run(service, db):
    return recon_scenarios.run(service, FakeConnection(db), "leafy_bank_bian", now=_later(1))


def test_every_scenario_wire_settles_on_the_correspondent_nostro(service, db):
    _fund(db)
    out = _run(service, db)

    assert set(out["payments"]) == {"R1", "R1b", "R2", "R3", "R4"}
    assert out["settled"] == 5
    for key, p in out["payments"].items():
        doc = db["payments"].find_one({"paymentId": p["paymentId"]})
        assert doc["lifecycle"]["settlementStatus"] == "SETTLED", key
        assert doc["clearing"]["settlementAccountCode"] == "1111", key
        assert doc["simulatedStatementOutcome"] == p["statementOutcome"]
        assert doc["chargeBearer"] == p["chargeBearer"]


def test_the_statement_carries_each_lever_and_holds_the_late_line(service, db):
    _fund(db)
    out = _run(service, db)
    stmt = db["paymentMessages"].find_one({"paymentMessageId": out["statement"]["paymentMessageId"]})
    by_pid = {e["simulatedPaymentId"]: e for e in stmt["entries"]}
    p = {k: v["paymentId"] for k, v in out["payments"].items()}

    assert by_pid[p["R1"]]["charges"] == camt053.CORRESPONDENT_CHARGE
    assert by_pid[p["R1b"]]["charges"] == camt053.CORRESPONDENT_CHARGE
    assert by_pid[p["R2"]]["reference"] != p["R2"]
    assert p["R3"] not in by_pid, "LATE is held one statement"
    assert by_pid[p["R4"]]["amount"] != out["payments"]["R4"]["amount"]
    assert by_pid[p["R4"]]["charges"] is None
    assert out["R5"] and by_pid[None]["reference"].startswith(camt053.ORPHAN_REF_PREFIX)


def test_the_next_cycle_books_the_late_line_clean(service, db):
    _fund(db)
    out = _run(service, db)
    nxt = recon_scenarios.advance_cycle(service, FakeConnection(db), "leafy_bank_bian", now=_later(2))
    line = next(e for e in nxt["entries"] if e["simulatedPaymentId"] == out["payments"]["R3"]["paymentId"])
    assert line["amount"] == out["payments"]["R3"]["amount"]


def test_no_scenario_wire_is_held_for_fraud_review_or_flagged_duplicate(service, db):
    """The amounts are chosen to stay under REVIEW (see the module docstring)."""
    _fund(db)
    out = _run(service, db)
    for key, p in out["payments"].items():
        doc = db["payments"].find_one({"paymentId": p["paymentId"]})
        assert doc["fraud"]["decision"] == "APPROVED", (key, doc["fraud"])


def test_an_unfunded_bank_refuses_up_front(service, db):
    import pytest
    with pytest.raises(ValueError, match="fund the scenarios"):
        _run(service, db)
