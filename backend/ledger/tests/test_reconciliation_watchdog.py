"""Reconciliation plan A3 — the MISSING watchdog, orphan lines, upserted items, and the
per-payment re-run.

Fixtures reuse A2's (`test_statement_matching`), which mirror the transactions service's
writers. `expectedWindow` mirrors `settle._stamp_expected_window` ({from, by} at the SETTLED
flip). The cross-service chain (initiate → complete_due → statement generate → this) has no
hermetic harness — separate venvs — so its reachability is the live gate's job (plan step 9).
"""

from __future__ import annotations

from datetime import timedelta

from routers import pipeline
from services.exceptions_service import (
    CATEGORY_ORPHANED_SETTLEMENT,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_RECONCILIATION_MISSING,
)
from services.reconciliation_service import reconcile_settled_payments
from services.statement_matching import match_statements, orphan_key, raise_orphans
from tests._fakedb import FakeConnection
from tests.test_statement_matching import _NOW, _line, _statement, _wire_fixture

_BY = _NOW + timedelta(seconds=600)
_LATE = _BY + timedelta(seconds=1)


def _missing_wire(c, *, line_amount=25000.0, window=True):
    """A settled outbound wire whose statement has NOT arrived: A2's wire fixture with the
    statement removed and the position's `expectedWindow` stamped."""
    _wire_fixture(c, line_amount)
    line = c.get_collection("db", "paymentMessages").docs.pop()["entries"][0]
    if window:
        c.get_collection("db", "settlementPositions").update_one(
            {"paymentId": "PAY-A"}, {"$set": {"expectedWindow": {"from": _NOW, "by": _BY}}})
    return line


def _arrive(c, line):
    c.seed("paymentMessages", [_statement([line])])


def _excs(c, category=None):
    docs = c.get_collection("db", "exceptions").docs
    return [d for d in docs if category is None or d["category"] == category]


def _items(c):
    return c.get_collection("db", "reconciliationItems").docs


# --- RECONCILIATION_MISSING ----------------------------------------------------

def test_a_wire_inside_its_window_is_pending_with_no_exception():
    c = FakeConnection()
    _missing_wire(c)

    result = reconcile_settled_payments(c, "db", now=_NOW + timedelta(seconds=60))

    assert result["pending"] == 1 and result["missing"] == 0
    assert _excs(c) == []


def test_a_wire_past_its_window_raises_one_missing_and_stays_unreconciled():
    c = FakeConnection()
    _missing_wire(c)

    first = reconcile_settled_payments(c, "db", now=_LATE)
    reconcile_settled_payments(c, "db", now=_LATE + timedelta(seconds=600))

    assert first["missing"] == 1
    missing = _excs(c, CATEGORY_RECONCILIATION_MISSING)
    assert len(missing) == 1                                   # OPEN dedupe across batches
    assert missing[0]["paymentId"] == "PAY-A"
    assert missing[0]["detail"]["expectedWindowBy"] == _BY
    assert missing[0]["detail"]["expectedAmount"] == 25000.0
    payment = c.get_collection("db", "payments").find_one({"paymentId": "PAY-A"})
    assert payment["lifecycle"].get("reconciliationStatus") is None   # not DISCREPANT
    assert len(_items(c)) == 1 and _items(c)[0]["overallResult"] == "PENDING"


def test_a_position_without_an_expected_window_is_never_raised():
    """Pre-A2 positions carry no window."""
    c = FakeConnection()
    _missing_wire(c, window=False)

    assert reconcile_settled_payments(c, "db", now=_LATE)["missing"] == 0
    assert _excs(c) == []


def test_an_inbound_wire_is_never_raised_missing():
    c = FakeConnection()
    _missing_wire(c)
    c.get_collection("db", "payments").update_one({"paymentId": "PAY-A"}, {"$set": {"direction": "INBOUND"}})
    c.get_collection("db", "settlementPositions").update_one(
        {"paymentId": "PAY-A"}, {"$set": {"actualAmount": 25000.0}})

    reconcile_settled_payments(c, "db", now=_LATE)

    assert _excs(c, CATEGORY_RECONCILIATION_MISSING) == []


def test_a_late_clean_line_reconciles_and_auto_resolves_the_missing():
    """D2 — the late line makes MISSING untrue; the sweep closes it with RECHECK."""
    c = FakeConnection()
    line = _missing_wire(c)
    reconcile_settled_payments(c, "db", now=_LATE)

    _arrive(c, line)
    match_statements(c, "db", now=_LATE)
    result = reconcile_settled_payments(c, "db", now=_LATE)

    assert result["reconciled"] == 1
    (missing,) = _excs(c, CATEGORY_RECONCILIATION_MISSING)
    assert missing["status"] == "RESOLVED"
    assert missing["resolution"]["action"] == "RECHECK"
    assert missing["resolution"]["by"] == "ledger-service"


def test_a_late_short_line_resolves_the_missing_and_opens_a_discrepancy():
    c = FakeConnection()
    line = _missing_wire(c)
    reconcile_settled_payments(c, "db", now=_LATE)

    _arrive(c, {**line, "amount": 24975.0, "charges": 25.0})
    match_statements(c, "db", now=_LATE)
    result = reconcile_settled_payments(c, "db", now=_LATE)

    assert result["discrepant"] == 1
    assert _excs(c, CATEGORY_RECONCILIATION_MISSING)[0]["status"] == "RESOLVED"
    (disc,) = _excs(c, CATEGORY_RECONCILIATION_DISCREPANCY)
    assert disc["status"] == "OPEN"
    assert disc["detail"]["discrepancyAmount"] == 25.0


# --- reconciliationItems upsert ------------------------------------------------

def test_re_checking_a_discrepant_payment_updates_one_item_in_place():
    c = FakeConnection()
    _wire_fixture(c, 24975.0)
    match_statements(c, "db", now=_NOW)

    reconcile_settled_payments(c, "db", now=_NOW)
    first_id = _items(c)[0]["reconciliationItemId"]
    reconcile_settled_payments(c, "db", now=_NOW)
    reconcile_settled_payments(c, "db", now=_NOW)

    assert len(_items(c)) == 1
    assert _items(c)[0]["reconciliationItemId"] == first_id
    payment = c.get_collection("db", "payments").find_one({"paymentId": "PAY-A"})
    assert payment["refs"]["reconciliationItemId"] == first_id


def test_a_missing_item_becomes_the_reconciled_item():
    """The overdue PENDING item is the open item the later RECONCILED check lands on."""
    c = FakeConnection()
    line = _missing_wire(c)
    reconcile_settled_payments(c, "db", now=_LATE)
    _arrive(c, line)
    match_statements(c, "db", now=_LATE)
    reconcile_settled_payments(c, "db", now=_LATE)

    assert [i["overallResult"] for i in _items(c)] == ["RECONCILED"]


# --- ORPHANED_SETTLEMENT -------------------------------------------------------

def _orphans(c):
    return _excs(c, CATEGORY_ORPHANED_SETTLEMENT)


def test_an_orphan_line_raises_one_exception_keyed_by_the_line():
    c = FakeConnection()
    c.seed("paymentMessages", [_statement([_line(1, "ORPH-1A2B3C4D", 3400.0, simulated_outcome="ORPHAN")])])

    raise_orphans(c, "db")
    raise_orphans(c, "db")

    (exc,) = _orphans(c)
    assert exc["paymentId"] == orphan_key("PM-STMT0001", 1) == "PM-STMT0001#1"
    assert exc["subjectRef"] == {"kind": "STATEMENT_LINE", "paymentMessageId": "PM-STMT0001", "lineNo": 1}
    assert exc["detail"]["actualAmount"] == 3400.0
    assert exc["detail"]["reference"] == "ORPH-1A2B3C4D"
    stmt = c.get_collection("db", "paymentMessages").docs[0]
    assert stmt["entries"][0]["recon"]["exceptionId"] == exc["exceptionId"]
    assert stmt["entries"][0]["recon"]["status"] == "UNMATCHED"   # still unmatched — A4 links it


def test_two_orphans_on_one_statement_raise_two_exceptions():
    """D1a — per-line keys, so the OPEN-unique index cannot collide two orphans."""
    c = FakeConnection()
    c.seed("paymentMessages", [_statement([_line(1, "ORPH-A", 1250.0), _line(2, "ORPH-B", 7800.0)])])

    assert raise_orphans(c, "db") == {"orphansRaised": 2}
    assert {e["paymentId"] for e in _orphans(c)} == {"PM-STMT0001#1", "PM-STMT0001#2"}


def test_a_matched_line_is_never_raised_as_an_orphan():
    c = FakeConnection()
    _wire_fixture(c, 25000.0)
    match_statements(c, "db", now=_NOW)

    assert raise_orphans(c, "db") == {"orphansRaised": 0}


def test_an_altered_reference_raises_the_orphan_now_and_the_missing_after_the_window():
    """D3 — the twin pair A4's LINK_STATEMENT_ENTRY will close."""
    c = FakeConnection()
    line = _missing_wire(c)
    _arrive(c, {**line, "reference": "8EBAA746/LEAFYBK", "simulatedOutcome": "REFERENCE_ALTERED"})

    match_statements(c, "db", now=_NOW)
    raise_orphans(c, "db")
    reconcile_settled_payments(c, "db", now=_NOW)
    assert len(_orphans(c)) == 1 and _excs(c, CATEGORY_RECONCILIATION_MISSING) == []

    reconcile_settled_payments(c, "db", now=_LATE)
    assert len(_excs(c, CATEGORY_RECONCILIATION_MISSING)) == 1


def test_orphan_raising_never_reads_simulator_truth():
    """Same guard as A2's: the `simulated*` keys are absent from the whole module."""
    import inspect
    import services.statement_matching as m
    src = inspect.getsource(m.raise_orphans)
    assert "simulatedPaymentId" not in src and "simulatedOutcome" not in src


# --- POST /pipeline/reconcile/{paymentId} -------------------------------------

def _call(c, payment_id, match=False):
    """Calls the route handler directly (no httpx in this venv for TestClient)."""
    import json
    from types import SimpleNamespace
    from fastapi import HTTPException
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(connection=c, db_name="db")))
    try:
        resp = pipeline.reconcile_payment(payment_id, req, match=match)
    except HTTPException as exc:
        return exc.status_code, None
    return resp.status_code, json.loads(resp.body)


def test_the_route_reconciles_one_payment_after_matching():
    c = FakeConnection()
    _wire_fixture(c, 25000.0)

    status, body = _call(c, "PAY-A", match=True)

    assert status == 200
    assert body["outcome"] == "RECONCILED"
    assert body["check"]["legs"][1]["result"] == "MATCH"


def test_the_route_without_match_leaves_the_leg_waiting():
    c = FakeConnection()
    _wire_fixture(c, 25000.0)

    _, body = _call(c, "PAY-A")

    assert body["outcome"] == "PENDING"
    assert body["check"]["legs"][1]["reason"] == "AWAITING_STATEMENT"


def test_the_route_refuses_a_reconciled_payment_and_an_unknown_one():
    c = FakeConnection()
    _wire_fixture(c, 25000.0)
    _call(c, "PAY-A", match=True)

    assert _call(c, "PAY-A")[0] == 409
    assert _call(c, "PAY-NOPE")[0] == 404


# --- plan A4 D2: reconciliation closes a POST_ADJUSTMENT itself ----------------

def _adj_event(amount_minor=2500):
    return {"eventId": "EVT-ADJ", "idempotencyKey": "PAY-A-ADJ", "postingStatus": "POSTED",
            "debitLeg": {"glAccountCode": "5214", "amount": amount_minor, "currency": "USD"},
            "creditLeg": {"glAccountCode": "1111", "amount": amount_minor, "currency": "USD"}}


def _fee_wire(c):
    """A DEBT fee wire matched short (24,975 vs 25,000) and already DISCREPANT."""
    _wire_fixture(c, 24975.0)
    match_statements(c, "db", now=_NOW)
    reconcile_settled_payments(c, "db", now=_NOW)


def test_an_approved_adjustment_awaiting_its_event_raises_no_fresh_discrepancy():
    c = FakeConnection()
    _fee_wire(c)
    exc = c.get_collection("db", "exceptions")
    exc.update_one({"category": CATEGORY_RECONCILIATION_DISCREPANCY}, {"$set": {"status": "RESOLVED"}})
    c.get_collection("db", "settlementPositions").update_one(
        {"paymentId": "PAY-A"}, {"$set": {"adjustmentPending": True}})

    for _ in range(2):
        result = reconcile_settled_payments(c, "db", now=_NOW)

    assert result["pending"] == 1 and result["discrepant"] == 0
    assert [e["status"] for e in _excs(c, CATEGORY_RECONCILIATION_DISCREPANCY)] == ["RESOLVED"]


def test_the_posted_adjustment_closes_leg_two_and_reconciles():
    c = FakeConnection()
    _fee_wire(c)
    c.get_collection("db", "payments").update_one(
        {"paymentId": "PAY-A"}, {"$set": {"lifecycle.reconciliationStatus": "DISCREPANT"}})
    c.seed("ledgerEvents", [_adj_event()])

    result = reconcile_settled_payments(c, "db", now=_NOW)

    assert result["reconciled"] == 1
    payment = c.get_collection("db", "payments").find_one({"paymentId": "PAY-A"})
    assert payment["lifecycle"]["reconciliationStatus"] == "RECONCILED"


def test_an_adjustment_of_the_wrong_amount_still_mismatches():
    c = FakeConnection()
    _fee_wire(c)
    c.seed("ledgerEvents", [_adj_event(1000)])

    assert reconcile_settled_payments(c, "db", now=_NOW)["discrepant"] == 1
