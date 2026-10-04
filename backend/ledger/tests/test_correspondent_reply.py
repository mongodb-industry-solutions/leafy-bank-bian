"""The correspondent's answer to an escalated exception (after a delay, or on demand)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from services import correspondent_reply, resolution_service
from services.exceptions_service import (
    CATEGORY_ORPHANED_SETTLEMENT,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_RECONCILIATION_MISSING,
)
from services.reconciliation_service import reconcile_settled_payments
from services.statement_matching import match_statements, raise_orphans
from tests._fakedb import FakeConnection
from tests.test_reconciliation_watchdog import _LATE, _excs, _missing_wire
from tests.test_statement_matching import _NOW, _line, _statement, _wire_fixture

_ESCALATED_AT = _NOW


def _escalate(c, exc):
    c.get_collection("db", "exceptions").update_one(
        {"exceptionId": exc["exceptionId"]},
        {"$set": {"awaitingCounterparty": True,
                  "escalation": {"paymentMessageId": "PM-CASE", "by": "payments-operations",
                                 "at": _ESCALATED_AT, "note": None}}})


def _discrepancy(c):
    _wire_fixture(c, 24975.0)
    match_statements(c, "db", now=_NOW)
    reconcile_settled_payments(c, "db", now=_LATE)
    (exc,) = _excs(c, CATEGORY_RECONCILIATION_DISCREPANCY)
    _escalate(c, exc)
    return exc


def test_the_reply_confirms_the_amount_and_the_recheck_closes_the_discrepancy():
    c = FakeConnection()
    exc = _discrepancy(c)

    result = correspondent_reply.reply(c, "db", exc["exceptionId"])

    assert result["reply"] == "AMOUNT_CONFIRMED"
    closed = result["exception"]
    assert closed["status"] == "RESOLVED"
    assert closed["resolution"]["by"] == correspondent_reply.CORRESPONDENT
    assert closed["awaitingCounterparty"] is False
    assert closed["escalation"]["reply"]["outcome"] == "AMOUNT_CONFIRMED"
    payment = c.get_collection("db", "payments").find_one({"paymentId": "PAY-A"})
    assert payment["lifecycle"]["reconciliationStatus"] == "RECONCILED"


def test_the_reply_supplies_a_missing_line():
    c = FakeConnection()
    _missing_wire(c)
    reconcile_settled_payments(c, "db", now=_LATE)
    (missing,) = _excs(c, CATEGORY_RECONCILIATION_MISSING)
    _escalate(c, missing)

    result = correspondent_reply.reply(c, "db", missing["exceptionId"])

    assert result["exception"]["status"] == "RESOLVED"


def test_the_reply_dismisses_an_orphan_line():
    c = FakeConnection()
    _missing_wire(c)
    c.seed("paymentMessages", [_statement([_line(1, "NOT-A-PAYMENT", 500.0)])])
    match_statements(c, "db", now=_LATE)
    raise_orphans(c, "db")
    (orphan,) = _excs(c, CATEGORY_ORPHANED_SETTLEMENT)
    _escalate(c, orphan)

    result = correspondent_reply.reply(c, "db", orphan["exceptionId"])

    assert result["exception"]["status"] == "DISMISSED"
    assert result["exception"]["resolution"]["action"] == "DISMISS"


def test_an_exception_that_was_never_escalated_is_refused():
    c = FakeConnection()
    exc = _discrepancy(c)
    c.get_collection("db", "exceptions").update_one(
        {"exceptionId": exc["exceptionId"]}, {"$set": {"awaitingCounterparty": False}})

    with pytest.raises(resolution_service.Conflict):
        correspondent_reply.reply(c, "db", exc["exceptionId"])


def test_a_second_reply_is_refused():
    c = FakeConnection()
    exc = _discrepancy(c)
    correspondent_reply.reply(c, "db", exc["exceptionId"])

    with pytest.raises(resolution_service.Conflict):
        correspondent_reply.reply(c, "db", exc["exceptionId"])


def test_only_escalations_older_than_the_delay_are_answered():
    c = FakeConnection()
    exc = _discrepancy(c)

    too_soon = _ESCALATED_AT + timedelta(seconds=19)
    assert correspondent_reply.reply_due(c, "db", delay_seconds=20, now=too_soon) == []
    assert _excs(c, CATEGORY_RECONCILIATION_DISCREPANCY)[0]["status"] == "OPEN"

    due = _ESCALATED_AT + timedelta(seconds=20)
    assert correspondent_reply.reply_due(c, "db", delay_seconds=20, now=due) == [exc["exceptionId"]]
    assert _excs(c, CATEGORY_RECONCILIATION_DISCREPANCY)[0]["status"] == "RESOLVED"
