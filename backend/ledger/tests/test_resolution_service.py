"""Plan A4 — the ledger-run resolution actions (RECHECK, LINK_STATEMENT_ENTRY).

Fixtures reuse A2/A3's, which mirror the transactions service's writers. REFERENCE_ALTERED
is modelled as a line whose reference does not resolve: it becomes an orphan, and its
payment goes MISSING once the window lapses — the twin pair LINK closes.
"""

from __future__ import annotations

import pytest

from services.exceptions_service import (
    CATEGORY_ORPHANED_SETTLEMENT,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_RECONCILIATION_MISSING,
)
from services import resolution_service
from services.reconciliation_service import reconcile_settled_payments
from services.statement_matching import match_statements, raise_orphans
from tests._fakedb import FakeConnection
from tests.test_reconciliation_watchdog import _LATE, _arrive, _excs, _missing_wire
from tests.test_statement_matching import _line


def _twins(c, *, amount=25000.0):
    """A re-keyed line (orphan) and its payment (MISSING), both OPEN."""
    _missing_wire(c)
    _arrive(c, _line(1, "8EBAA746/LEAFYBK", amount))
    match_statements(c, "db", now=_LATE)
    raise_orphans(c, "db")
    reconcile_settled_payments(c, "db", now=_LATE)
    (missing,) = _excs(c, CATEGORY_RECONCILIATION_MISSING)
    (orphan,) = _excs(c, CATEGORY_ORPHANED_SETTLEMENT)
    return missing, orphan


def _status(c, pid="PAY-A"):
    p = c.get_collection("db", "payments").find_one({"paymentId": pid})
    return p["lifecycle"].get("reconciliationStatus")


# --- RECHECK -------------------------------------------------------------------

def test_recheck_with_nothing_new_leaves_the_missing_open():
    c = FakeConnection()
    _missing_wire(c)
    reconcile_settled_payments(c, "db", now=_LATE)
    (missing,) = _excs(c, CATEGORY_RECONCILIATION_MISSING)

    result = resolution_service.recheck(c, "db", missing["exceptionId"])

    assert result["outcome"] in ("PENDING", "MISSING")
    assert result["exception"]["status"] == "OPEN"


def test_recheck_after_the_line_arrives_resolves_with_recheck():
    c = FakeConnection()
    line = _missing_wire(c)
    reconcile_settled_payments(c, "db", now=_LATE)
    (missing,) = _excs(c, CATEGORY_RECONCILIATION_MISSING)
    _arrive(c, line)

    result = resolution_service.recheck(c, "db", missing["exceptionId"], by="reconciliation-agent")

    assert result["outcome"] == "RECONCILED"
    assert result["exception"]["status"] == "RESOLVED"
    assert result["exception"]["resolution"]["action"] == "RECHECK"
    assert _status(c) == "RECONCILED"


def test_recheck_is_refused_on_an_orphan():
    c = FakeConnection()
    _, orphan = _twins(c)
    with pytest.raises(resolution_service.NotLegal):
        resolution_service.recheck(c, "db", orphan["exceptionId"])


# --- LINK_STATEMENT_ENTRY --------------------------------------------------------

@pytest.mark.parametrize("from_side", ["orphan", "missing"])
def test_link_closes_both_twins_and_reconciles(from_side):
    c = FakeConnection()
    missing, orphan = _twins(c)
    if from_side == "orphan":
        result = resolution_service.link(c, "db", orphan["exceptionId"], payment_id="PAY-A")
    else:
        sub = orphan["subjectRef"]
        result = resolution_service.link(c, "db", missing["exceptionId"],
                                         payment_message_id=sub["paymentMessageId"],
                                         line_no=sub["lineNo"])

    assert result["outcome"] == "RECONCILED"
    for exc in _excs(c):
        assert exc["status"] == "RESOLVED"
        assert exc["resolution"]["action"] == "LINK_STATEMENT_ENTRY"
    stmt = c.get_collection("db", "paymentMessages").find_one({"purpose": "ACCOUNT_STATEMENT"})
    assert stmt["entries"][0]["recon"]["status"] == "MANUAL_MATCHED"
    assert stmt["entries"][0]["recon"]["matchedPaymentId"] == "PAY-A"
    # A later matching pass must not re-raise the line.
    raise_orphans(c, "db")
    assert len(_excs(c, CATEGORY_ORPHANED_SETTLEMENT)) == 1


def test_linking_a_short_line_reconciles_nothing_and_raises_the_discrepancy():
    c = FakeConnection()
    _, orphan = _twins(c, amount=24975.0)

    result = resolution_service.link(c, "db", orphan["exceptionId"], payment_id="PAY-A")

    assert result["outcome"] == "DISCREPANT"
    assert len(_excs(c, CATEGORY_RECONCILIATION_DISCREPANCY)) == 1


def test_link_refuses_a_line_on_another_nostro():
    c = FakeConnection()
    _, orphan = _twins(c)
    c.get_collection("db", "settlementPositions").update_one(
        {"paymentId": "PAY-A"}, {"$set": {"settlementAccountCode": "1121"}})
    with pytest.raises(resolution_service.NotLegal, match="nostro"):
        resolution_service.link(c, "db", orphan["exceptionId"], payment_id="PAY-A")
    assert all(e["status"] == "OPEN" for e in _excs(c))


def test_link_refuses_a_payment_that_already_has_an_actual():
    c = FakeConnection()
    _, orphan = _twins(c)
    c.get_collection("db", "settlementPositions").update_one(
        {"paymentId": "PAY-A"}, {"$set": {"actualAmount": 25000.0}})
    with pytest.raises(resolution_service.Conflict):
        resolution_service.link(c, "db", orphan["exceptionId"], payment_id="PAY-A")


def test_a_failed_link_rolls_back_both_claims():
    c = FakeConnection(raise_on="paymentMessages")
    missing, orphan = _twins_without_raising(c)
    with pytest.raises(Exception):
        resolution_service.link(c, "db", orphan["exceptionId"], payment_id="PAY-A")
    assert c.rolled_back
    assert all(e["status"] == "OPEN" for e in _excs(c))
    position = c.get_collection("db", "settlementPositions").find_one({"paymentId": "PAY-A"})
    assert position["actualAmount"] is None


def _twins_without_raising(c):
    raise_on, c.raise_on = c.raise_on, None
    twins = _twins(c)
    c.raise_on = raise_on
    return twins
