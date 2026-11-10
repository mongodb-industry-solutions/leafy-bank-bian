"""The correspondent's side of an escalated exception (after a simulated delay, or on demand).

Doina, 2026-11-10: the camt.111 reply is not accounting evidence. It never moves the booked
amount; only the correspondent's corrected camt.053 does, and only then does the exception
close.
"""

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
    c.seed("canonicalJsonStorage", [{
        "_id": "oid-case", "paymentMessageId": "PM-CASE", "paymentId": exc.get("paymentId"),
        "purpose": "INVESTIGATION_REQUEST", "messageFormat": "camt.110.001.01", "caseId": "CASE-1",
        "payload": {"Document": {"InvstgtnReq": {"InvstgtnReq": {
            "MsgId": "MSG-CASE", "RqstrInvstgtnId": "CASE-1",
            "InvstgtnTp": {"Prtry": "AMOUNT_DISCREPANCY"},
            "Rqstr": {"Agt": {"FinInstnId": {"BICFI": "LEAFUS33"}}},
            "Rspndr": {"Agt": {"FinInstnId": {"BICFI": "ROYCCAT2"}}}}}}},
    }])
    c.get_collection("db", "exceptions").update_one(
        {"exceptionId": exc["exceptionId"]},
        {"$set": {"awaitingCounterparty": True,
                  "escalation": {"paymentMessageId": "PM-CASE", "caseId": "CASE-1",
                                 "by": "payments-operations", "at": _ESCALATED_AT,
                                 "note": None}}})


def _corrected_statement(c, exc, *, booked=25000.0, original_amount=24975.0, missing=False):
    """What the transactions gateway's `issue_corrected_statement` writes."""
    ref = None if missing else {"paymentMessageId": "PM-STMT0001", "lineNo": 1}
    rebook = {**_line(2 if not missing else 1, "PAY-A", booked), "correctsEntry": ref}
    entries = [rebook] if missing else [
        {**_line(1, "PAY-A", original_amount), "creditDebit": "CRDT", "reversal": True,
         "reversesEntry": ref}, rebook]
    c.seed("canonicalJsonStorage", [{
        **_statement(entries), "_id": "oid-stmt-2", "paymentMessageId": "PM-STMT0002",
        "statement": {"accountCode": "1111", "currency": "USD", "sequence": 2,
                      "correction": {"exceptionId": exc["exceptionId"], "corrects": ref}},
    }])


def _discrepancy(c):
    _wire_fixture(c, 24975.0)
    match_statements(c, "db", now=_NOW)
    reconcile_settled_payments(c, "db", now=_LATE)
    (exc,) = _excs(c, CATEGORY_RECONCILIATION_DISCREPANCY)
    _escalate(c, exc)
    return exc


def _position(c):
    return c.get_collection("db", "settlementPositions").find_one({"paymentId": "PAY-A"})


def test_the_reply_is_a_camt111_and_leaves_the_books_and_the_exception_alone():
    c = FakeConnection()
    exc = _discrepancy(c)

    result = correspondent_reply.reply(c, "db", exc["exceptionId"])

    assert result["reply"] == "AMOUNT_CONFIRMED"
    pending = result["exception"]
    assert pending["status"] == "OPEN"
    assert pending["awaitingCounterparty"] is False
    assert pending["awaitingCorrectedStatement"] is True
    assert pending["escalation"]["reply"]["confirmedAmount"] == 25000.0
    assert _position(c)["actualAmount"] == 24975.0          # still what was booked
    payment = c.get_collection("db", "payments").find_one({"paymentId": "PAY-A"})
    assert payment["lifecycle"]["reconciliationStatus"] != "RECONCILED"

    response = c.get_collection("db", "canonicalJsonStorage").find_one(
        {"purpose": "INVESTIGATION_RESPONSE"})
    assert response["messageFormat"] == "camt.111.001.01"
    assert response["caseId"] == "CASE-1" and response["inResponseTo"] == "PM-CASE"
    body = response["payload"]["Document"]["InvstgtnRspn"]
    assert body["OrgnlInvstgtnReq"]["RqstrInvstgtnId"] == "CASE-1"
    assert pending["escalation"]["reply"]["paymentMessageId"] == response["paymentMessageId"]
    assert "<InvstgtnRspn>" in response["rawMessage"]


def test_without_a_corrected_statement_the_exception_cannot_close():
    c = FakeConnection()
    exc = _discrepancy(c)
    correspondent_reply.reply(c, "db", exc["exceptionId"])

    with pytest.raises(resolution_service.Conflict, match="corrected statement"):
        correspondent_reply.apply_corrected_statement(c, "db", exc["exceptionId"])
    assert correspondent_reply.apply_due(c, "db") == []
    assert _excs(c, CATEGORY_RECONCILIATION_DISCREPANCY)[0]["status"] == "OPEN"


def test_the_corrected_statement_rebooks_the_position_and_closes_the_discrepancy():
    c = FakeConnection()
    exc = _discrepancy(c)
    correspondent_reply.reply(c, "db", exc["exceptionId"])
    _corrected_statement(c, exc)

    result = correspondent_reply.apply_corrected_statement(c, "db", exc["exceptionId"])

    closed = result["exception"]
    assert closed["status"] == "RESOLVED"
    assert closed["resolution"]["by"] == correspondent_reply.CORRESPONDENT
    assert "PM-STMT0002" in closed["resolution"]["note"]
    assert closed["awaitingCorrectedStatement"] is False
    position = _position(c)
    assert position["actualAmount"] == 25000.0
    assert position["sourceMessageRef"] == "PM-STMT0002"
    assert position["correctedFrom"]["amount"] == 24975.0
    payment = c.get_collection("db", "payments").find_one({"paymentId": "PAY-A"})
    assert payment["lifecycle"]["reconciliationStatus"] == "RECONCILED"
    stmts = {d["paymentMessageId"]: d for d in c.get_collection("db", "canonicalJsonStorage").docs
             if d.get("purpose") == "ACCOUNT_STATEMENT"}
    assert stmts["PM-STMT0001"]["entries"][0]["recon"]["reversedBy"]["paymentMessageId"] == "PM-STMT0002"
    assert [e["recon"]["matchedBy"] for e in stmts["PM-STMT0002"]["entries"]] == ["REVERSAL", "CORRECTION"]


def test_a_corrected_statement_that_still_disagrees_keeps_the_exception_open():
    c = FakeConnection()
    exc = _discrepancy(c)
    correspondent_reply.reply(c, "db", exc["exceptionId"])
    _corrected_statement(c, exc, booked=24990.0)

    result = correspondent_reply.apply_corrected_statement(c, "db", exc["exceptionId"])

    assert result["exception"]["status"] == "OPEN"
    # Applied once: nothing is pending, so the worker does not re-run the recheck forever.
    assert result["exception"]["awaitingCorrectedStatement"] is False
    assert result["exception"]["escalation"]["correctedStatement"]["bookedAmount"] == 24990.0
    assert correspondent_reply.apply_due(c, "db") == []
    with pytest.raises(resolution_service.Conflict, match="not awaiting"):
        correspondent_reply.apply_corrected_statement(c, "db", exc["exceptionId"])


def test_the_reply_then_the_statement_supply_a_missing_line():
    c = FakeConnection()
    _missing_wire(c)
    reconcile_settled_payments(c, "db", now=_LATE)
    (missing,) = _excs(c, CATEGORY_RECONCILIATION_MISSING)
    _escalate(c, missing)

    assert correspondent_reply.reply(c, "db", missing["exceptionId"])["exception"]["status"] == "OPEN"
    _corrected_statement(c, missing, missing=True)

    assert correspondent_reply.apply_due(c, "db") == [missing["exceptionId"]]
    assert _excs(c, CATEGORY_RECONCILIATION_MISSING)[0]["status"] == "RESOLVED"


def test_the_reply_dismisses_an_orphan_line():
    c = FakeConnection()
    _missing_wire(c)
    c.seed("canonicalJsonStorage", [_statement([_line(1, "NOT-A-PAYMENT", 500.0)])])
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


def test_the_loser_of_a_reply_race_leaves_no_second_camt111(monkeypatch):
    c = FakeConnection()
    exc = _discrepancy(c)
    stale = dict(exc)
    correspondent_reply.reply(c, "db", exc["exceptionId"])

    monkeypatch.setattr(correspondent_reply, "_awaiting", lambda *_: stale)
    with pytest.raises(resolution_service.Conflict, match="another caller"):
        correspondent_reply.reply(c, "db", exc["exceptionId"])

    docs = c.get_collection("db", "canonicalJsonStorage").docs
    assert sum(d.get("purpose") == "INVESTIGATION_RESPONSE" for d in docs) == 1


def test_a_correction_line_that_cannot_apply_yet_is_not_queued_as_an_orphan():
    c = FakeConnection()
    exc = _discrepancy(c)
    correspondent_reply.reply(c, "db", exc["exceptionId"])
    _corrected_statement(c, exc)
    # The position no longer cites the original statement, so the rebooking cannot apply.
    c.get_collection("db", "settlementPositions").update_one(
        {"paymentId": "PAY-A"}, {"$set": {"sourceMessageRef": "PM-OTHER"}})

    match_statements(c, "db", now=_NOW)
    raise_orphans(c, "db")

    orphans = [e for e in _excs(c, "ORPHANED_SETTLEMENT")
               if str(e["paymentId"]).startswith("PM-STMT0002#")]
    assert orphans == []


def test_statements_are_matched_oldest_first():
    from services.statement_matching import _by_sequence

    stmts = [{"statement": {"sequence": 3}}, {"statement": {"sequence": 1}}, {"statement": {}}]
    assert [s["statement"].get("sequence") for s in _by_sequence(stmts)] == [None, 1, 3]


def test_only_escalations_older_than_the_delay_are_answered():
    c = FakeConnection()
    exc = _discrepancy(c)

    too_soon = _ESCALATED_AT + timedelta(seconds=19)
    assert correspondent_reply.reply_due(c, "db", delay_seconds=20, now=too_soon) == []
    assert _excs(c, CATEGORY_RECONCILIATION_DISCREPANCY)[0]["status"] == "OPEN"

    due = _ESCALATED_AT + timedelta(seconds=20)
    assert correspondent_reply.reply_due(c, "db", delay_seconds=20, now=due) == [exc["exceptionId"]]
    answered = _excs(c, CATEGORY_RECONCILIATION_DISCREPANCY)[0]
    assert answered["status"] == "OPEN" and answered["awaitingCorrectedStatement"] is True
