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


# --- Plan E: the walkthrough's one-wire-per-click route -------------------------------------

def test_run_one_initiates_exactly_one_wire_with_its_lever(service, db):
    _fund(db)
    out = recon_scenarios.run_one(service, "R1")
    assert db["payments"].count_documents({}) == 1
    doc = db["payments"].find_one({"paymentId": out["paymentId"]})
    assert doc["chargeBearer"] == "DEBT"
    assert doc["simulatedStatementOutcome"] == camt053.FEE_DEDUCTED
    assert doc["lifecycle"]["settlementStatus"] != "SETTLED", "no settle on initiate"
    assert out["bic"] == "BARCGB22" and out["includeOrphan"] is False


def test_r5_and_timing_lag_initiate_clean_wires(service, db):
    _fund(db)
    r5 = recon_scenarios.run_one(service, "R5")
    tl = recon_scenarios.run_one(service, "TL")
    assert r5["includeOrphan"] is True and tl["includeOrphan"] is False
    assert r5["statementOutcome"] == tl["statementOutcome"] == camt053.CLEAN
    assert tl["bic"] == "UBSWCHZH" and tl["chargeBearer"] == "SHAR"
    for p in (r5, tl):
        doc = db["payments"].find_one({"paymentId": p["paymentId"]})
        assert doc["fraud"]["decision"] == "APPROVED"


def test_run_one_funds_check_uses_only_that_scenarios_amount(service, db):
    _fund(db, amount=5_000.0)  # below TOTAL, above R1's 4,850
    assert recon_scenarios.run_one(service, "R1")["paymentId"]


def _request(service, db):
    from types import SimpleNamespace
    state = SimpleNamespace(payments_service=service, connection=FakeConnection(db),
                            db_name="leafy_bank_bian")
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_route_refuses_an_unknown_scenario_with_422(service, db):
    import pytest
    from fastapi import HTTPException
    from api_models import ReconScenarioRequest
    from routers.workflow import initiate_recon_scenario

    with pytest.raises(HTTPException) as e:
        initiate_recon_scenario(ReconScenarioRequest(scenario="R3"), _request(service, db))
    assert e.value.status_code == 422


def test_settle_due_route_settles_the_initiated_wire(service, db):
    import json
    from api_models import ReconScenarioRequest
    from routers.workflow import initiate_recon_scenario, settle_due

    _fund(db)
    req = _request(service, db)
    body = json.loads(initiate_recon_scenario(ReconScenarioRequest(scenario="R2"), req).body)
    assert json.loads(settle_due(req).body) == {"settled": 1}
    doc = db["payments"].find_one({"paymentId": body["paymentId"]})
    assert doc["lifecycle"]["settlementStatus"] == "SETTLED"
    assert set(body) == {"scenario", "paymentId", "status", "amount", "chargeBearer",
                         "statementOutcome", "bic", "bankName", "includeOrphan"}
