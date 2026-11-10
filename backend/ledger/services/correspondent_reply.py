"""The correspondent's side of an escalated exception (the simulator's missing half).

`ESCALATE_TO_CORRESPONDENT` sends a camt.110 investigation request and leaves the exception
OPEN with `awaitingCounterparty`. Nothing outside the demo would answer it on a timer, so this
module plays the correspondent in two steps, each after a **simulated** delay (worker) or on
demand (the Showcase buttons). The delays are demo pacing, not SWIFT response times.

1. **Reply** (`reply`): the correspondent's camt.111 investigation response, correlated to our
   camt.110 by case id.
   - Amount discrepancy / missing line: it confirms the amount we expected and says it will
     book the correction. A reply is not accounting evidence (Doina, 2026-11-10): the position
     and the books are untouched, and the exception stays OPEN with
     `awaitingCorrectedStatement` until the correspondent's statement books the amount.
   - Orphaned line: it confirms a misposting. The exception is dismissed.
2. **Corrected statement** (`apply_corrected_statement`): once the correspondent's correcting
   camt.053 exists (written by the transactions service's gateway, reversing the mis-booked
   line and booking the expected amount), the normal RECHECK runs. Statement matching moves
   the position's actual amount to the newly booked figure; the deterministic tie-out still
   decides, and the exception closes only if it agrees.

The ledger runs this because it owns the position and the exception's resolution.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId

from database.connection import MongoDBConnection
from services import resolution_service
from services.exceptions_service import (
    CATEGORY_ORPHANED_SETTLEMENT,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_RECONCILIATION_MISSING,
    STATUS_DISMISSED,
    STATUS_OPEN,
)
from shared.refs import derive_ref

logger = logging.getLogger(__name__)

CORRESPONDENT = "correspondent-sim"
DEFAULT_REPLY_SECONDS = 20  # simulated pacing, not an expected SWIFT response time
_DISMISS = "DISMISS"

# camt.111 — mirrored from the transactions gateway's message conventions.
RESPONSE_PURPOSE = "INVESTIGATION_RESPONSE"
RESPONSE_FORMAT = "camt.111.001.01"
_NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:camt.111.001.01"
_STATEMENT_PURPOSE = "ACCOUNT_STATEMENT"

OUTCOME_AMOUNT_CONFIRMED = "AMOUNT_CONFIRMED"
OUTCOME_MISPOSTING_CONFIRMED = "MISPOSTING_CONFIRMED"


def _awaiting(exc_coll, exception_id: str) -> dict:
    exc = resolution_service._open_exception(
        exc_coll, exception_id,
        {CATEGORY_RECONCILIATION_DISCREPANCY, CATEGORY_RECONCILIATION_MISSING,
         CATEGORY_ORPHANED_SETTLEMENT}, "CORRESPONDENT_REPLY")
    if not exc.get("awaitingCounterparty"):
        raise resolution_service.Conflict(
            f"Exception {exception_id} has not been escalated to the correspondent.")
    return exc


def _response_payload(request: dict, msg_id: str, outcome: str, confirmed: Optional[float],
                      currency: str, now: datetime) -> dict:
    """The camt.111 body. Not XSD-validated (same caveat as the gateway's camt.110)."""
    original = (((request.get("payload") or {}).get("Document") or {})
                .get("InvstgtnReq") or {}).get("InvstgtnReq") or {}
    data = {"AddtlInf": (
        "Booking error confirmed. The amount will be corrected on our next statement."
        if outcome == OUTCOME_AMOUNT_CONFIRMED else
        "Entry posted to your account in error. It will be reversed.")}
    if confirmed is not None:
        data = {"CnfrmdAmt": {"@Ccy": currency, "#text": confirmed}, **data}
    return {"Document": {"InvstgtnRspn": {
        "InvstgtnRspn": {
            "MsgId": msg_id,
            "CreDtTm": now,
            "InvstgtnSts": {"Prtry": outcome},
            "Rqstr": original.get("Rspndr"),
            "Rspndr": original.get("Rqstr"),
        },
        "OrgnlInvstgtnReq": {
            "MsgId": original.get("MsgId"),
            "RqstrInvstgtnId": original.get("RqstrInvstgtnId"),
            "InvstgtnTp": original.get("InvstgtnTp"),
        },
        "InvstgtnData": [data],
    }}}


def _to_xml(payload: dict) -> str:
    """Same `@attr` / `#text` convention as the gateway's serialisers."""
    from xml.etree import ElementTree as ET

    def append(parent, name, value):
        if value is None:
            return
        if isinstance(value, list):
            for item in value:
                append(parent, name, item)
            return
        if not isinstance(value, dict):
            ET.SubElement(parent, name).text = (
                f"{value:.2f}" if isinstance(value, float) else
                value.isoformat() if hasattr(value, "isoformat") else str(value))
            return
        el = ET.SubElement(parent, name)
        for k, v in value.items():
            if k.startswith("@"):
                el.set(k[1:], str(v))
            elif k == "#text":
                el.text = f"{v:.2f}" if isinstance(v, float) else str(v)
            else:
                append(el, k, v)

    root = ET.Element("Document", {"xmlns": _NAMESPACE})
    append(root, "InvstgtnRspn", payload["Document"]["InvstgtnRspn"])
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _response_doc(messages, exc: dict, outcome: str, confirmed: Optional[float],
                  currency: str, now: datetime) -> dict:
    escalation = exc.get("escalation") or {}
    request = messages.find_one({"paymentMessageId": escalation.get("paymentMessageId")}) or {}
    oid = ObjectId()
    payload = _response_payload(request, derive_ref("MSG", oid), outcome, confirmed, currency, now)
    return {
        "_id": oid,
        "paymentMessageId": derive_ref("PM", oid),
        "paymentId": request.get("paymentId"),
        "paymentExecutionId": None,
        "direction": "INBOUND",
        "purpose": RESPONSE_PURPOSE,
        "messageStandard": "ISO20022",
        "messageFormat": RESPONSE_FORMAT,
        "mappingVersion": "1.0.0",
        "payload": payload,
        "rawMessage": _to_xml(payload),
        "rawMessageRef": None,
        "caseId": escalation.get("caseId") or request.get("caseId"),
        "inResponseTo": escalation.get("paymentMessageId"),
        "exceptionId": exc.get("exceptionId"),
        "subjectRef": exc.get("subjectRef"),
        "transformationAudit": None,
        "simulated": True,
        "sourceSystem": CORRESPONDENT,
        "createdAt": now,
    }


def _claim_reply(messages, exc_coll, exc: dict, response: dict, reply: dict, extra: dict,
                 now: datetime) -> None:
    """Store the camt.111, then record the answer; conditional on still-OPEN-and-awaiting
    so a double trigger (worker plus button, or two deployments on this DB) replies once.

    The message goes in first: a crash between the two writes leaves the exception still
    awaiting (the next tick answers it) rather than answered with no camt.111 on file. The
    loser of a race removes its own message.
    """
    messages.insert_one(response)
    claim = exc_coll.update_one(
        {"_id": exc["_id"], "status": STATUS_OPEN, "awaitingCounterparty": True},
        {"$set": {"awaitingCounterparty": False, "escalation.reply": reply,
                  "updatedAt": now, **extra}})
    if not claim.matched_count:
        messages.delete_one({"_id": response["_id"]})
        raise resolution_service.Conflict(
            f"Exception {exc['exceptionId']} was answered by another caller.")


def reply(connection: MongoDBConnection, db_name: str, exception_id: str) -> dict:
    """The correspondent's camt.111 for one escalated exception. Returns `{reply, exception}`."""
    exc_coll = connection.get_collection(db_name, "exceptions")
    messages = connection.get_collection(db_name, "canonicalJsonStorage")
    exc = _awaiting(exc_coll, exception_id)
    now = datetime.now(timezone.utc)

    if exc["category"] == CATEGORY_ORPHANED_SETTLEMENT:
        response = _response_doc(messages, exc, OUTCOME_MISPOSTING_CONFIRMED, None, "USD", now)
        _claim_reply(messages, exc_coll, exc, response,
                     _reply_record(response, OUTCOME_MISPOSTING_CONFIRMED, now), {}, now)
        exc_coll.update_one(
            {"_id": exc["_id"], "status": STATUS_OPEN},
            {"$set": {"status": STATUS_DISMISSED, "resolution": {
                "action": _DISMISS, "by": CORRESPONDENT, "at": now,
                "note": "Correspondent's camt.111 confirmed the line was a misposting."},
                "updatedAt": now}})
        return {"reply": OUTCOME_MISPOSTING_CONFIRMED,
                "exception": exc_coll.find_one({"exceptionId": exception_id}, {"_id": 0})}

    position = connection.get_collection(db_name, "settlementPositions").find_one(
        {"paymentId": exc["paymentId"]}, sort=[("createdAt", -1)])
    if position is None:
        raise resolution_service.NotLegal(
            f"Payment {exc['paymentId']} has no settlement position to investigate.")
    expected = position.get("expectedAmount", position.get("grossAmount"))
    currency = position.get("expectedCurrency") or position.get("currency") or "USD"
    response = _response_doc(messages, exc, OUTCOME_AMOUNT_CONFIRMED, expected, currency, now)
    record = _reply_record(response, OUTCOME_AMOUNT_CONFIRMED, now) | {
        "confirmedAmount": expected, "currency": currency}
    # The reply changes nothing on the books: the booked amount moves only when a corrected
    # camt.053 books it (`apply_corrected_statement`).
    _claim_reply(messages, exc_coll, exc, response, record,
                 {"awaitingCorrectedStatement": True}, now)
    return {"reply": OUTCOME_AMOUNT_CONFIRMED,
            "exception": exc_coll.find_one({"exceptionId": exception_id}, {"_id": 0})}


def _reply_record(response: dict, outcome: str, now: datetime) -> dict:
    return {"by": CORRESPONDENT, "at": now, "outcome": outcome,
            "paymentMessageId": response["paymentMessageId"],
            "messageFormat": RESPONSE_FORMAT}


def _correction_statement(messages, exception_id: str) -> Optional[dict]:
    return messages.find_one(
        {"purpose": _STATEMENT_PURPOSE, "statement.correction.exceptionId": exception_id},
        {"paymentMessageId": 1, "entries": 1})


def apply_corrected_statement(connection: MongoDBConnection, db_name: str,
                              exception_id: str) -> dict:
    """Re-check once the correspondent's correcting camt.053 has arrived.

    Returns the recheck's `{outcome, check, exception}`. Refused (Conflict) while no
    corrected statement exists: the camt.111 alone never closes the exception.
    """
    exc_coll = connection.get_collection(db_name, "exceptions")
    messages = connection.get_collection(db_name, "canonicalJsonStorage")
    exc = exc_coll.find_one({"exceptionId": exception_id})
    if exc is None:
        raise resolution_service.NotFound(f"Exception {exception_id} not found.")
    if exc.get("status") != STATUS_OPEN or not exc.get("awaitingCorrectedStatement"):
        raise resolution_service.Conflict(
            f"Exception {exception_id} is not awaiting a corrected statement.")
    statement = _correction_statement(messages, exception_id)
    if statement is None:
        raise resolution_service.Conflict(
            f"No corrected statement has arrived for {exception_id}; the correspondent's "
            "reply alone does not change the booked amount.")
    booked = next((e.get("amount") for e in statement.get("entries") or []
                   if not e.get("reversal")), None)
    booked_text = f"{booked:,.2f}" if isinstance(booked, (int, float)) else "an unknown amount"
    result = resolution_service.recheck(
        connection, db_name, exception_id, by=CORRESPONDENT,
        note=(f"Corrected camt.053 {statement['paymentMessageId']} booked {booked_text}; "
              "the booked position now matches our books."))
    # The correction is applied exactly once. Whether or not the recheck closed the
    # exception, nothing is pending any more: leaving the flag set would make the worker
    # re-run the recheck on every tick for an exception that stays OPEN.
    exc_coll.update_one({"_id": exc["_id"]}, {"$set": {
        "awaitingCorrectedStatement": False,
        "escalation.correctedStatement": {"paymentMessageId": statement["paymentMessageId"],
                                          "bookedAmount": booked}}})
    result["exception"] = exc_coll.find_one({"exceptionId": exception_id}, {"_id": 0})
    return result


def due_exception_ids(connection: MongoDBConnection, db_name: str, *,
                      delay_seconds: int = DEFAULT_REPLY_SECONDS,
                      now: Optional[datetime] = None) -> list[str]:
    """Escalated exceptions whose request is at least `delay_seconds` old."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=delay_seconds)
    exc_coll = connection.get_collection(db_name, "exceptions")
    return [e["exceptionId"] for e in exc_coll.find(
        {"status": STATUS_OPEN, "awaitingCounterparty": True, "escalation.at": {"$lte": cutoff}},
        {"exceptionId": 1})]


def reply_due(connection: MongoDBConnection, db_name: str, *,
              delay_seconds: int = DEFAULT_REPLY_SECONDS,
              now: Optional[datetime] = None) -> list[str]:
    """Answer every due escalation. Returns the ids answered; a lost race is skipped."""
    answered = []
    for exception_id in due_exception_ids(connection, db_name, delay_seconds=delay_seconds, now=now):
        try:
            reply(connection, db_name, exception_id)
            answered.append(exception_id)
        except resolution_service.ResolutionError as e:
            logger.info("correspondent reply skipped for %s: %s", exception_id, e)
    return answered


def apply_due(connection: MongoDBConnection, db_name: str) -> list[str]:
    """Re-check every exception whose corrected statement has arrived. Returns those closed."""
    exc_coll = connection.get_collection(db_name, "exceptions")
    messages = connection.get_collection(db_name, "canonicalJsonStorage")
    closed = []
    for exc in exc_coll.find({"status": STATUS_OPEN, "awaitingCorrectedStatement": True},
                             {"exceptionId": 1}):
        if _correction_statement(messages, exc["exceptionId"]) is None:
            continue
        try:
            result = apply_corrected_statement(connection, db_name, exc["exceptionId"])
        except resolution_service.ResolutionError as e:
            logger.info("corrected statement not applied for %s: %s", exc["exceptionId"], e)
            continue
        if (result.get("exception") or {}).get("status") != STATUS_OPEN:
            closed.append(exc["exceptionId"])
    return closed
