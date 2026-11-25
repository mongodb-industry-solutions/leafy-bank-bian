"""Reconciliation exception -> ISO 20022 camt.110.001.01 (InvestigationRequestV01). Pure.

The investigation request `ESCALATE_TO_CORRESPONDENT` sends. camt.110 / camt.111 is the
SWIFT Case Management path for cross-border exceptions and investigations: camt.110 is the
request, camt.111 the correspondent's response. It replaces camt.026 (UnableToApply), a
plausible legacy fit whose meaning — "unable to apply" — does not describe "your booked
amount differs from ours" (Doina, 2026-11-10). SWIFT starts camt.110 reception with in-flow
translation in November 2026; camt.110/111 exchange through Case Management is planned to
become mandatory in November 2027.

The request cites the underlying payment by UETR and end-to-end id with its interbank
amount, or, for an orphan line, the statement entry. Investigation data carries what we
expected against what was booked, so the correspondent can answer without a second query.

## Same provenance caveat as pacs002.py / camt053.py

No ISO schema is available in the workspace; the structure is from the published camt.110
message definition and is **not XSD-validated**. The investigation type is proprietary
(`Prtry`) because the demo does not ship the external code list.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from contexts.payment_order_initiation.domain import bank_identity
from contexts.payment_rail.domain import pacs008

MESSAGE_FORMAT = "camt.110.001.01"
MESSAGE_STANDARD = "ISO20022"
MAPPING_VERSION = "1.0.0"

ROOT = pacs008.ROOT
MESSAGE_ROOT = "InvstgtnReq"
NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:camt.110.001.01"

# Our proprietary investigation types, one per escalated exception category.
TYPE_AMOUNT_DISCREPANCY = "AMOUNT_DISCREPANCY"
TYPE_MISSING_ENTRY = "MISSING_STATEMENT_ENTRY"
TYPE_UNKNOWN_ENTRY = "UNKNOWN_STATEMENT_ENTRY"
_TYPE_BY_CATEGORY = {
    "RECONCILIATION_DISCREPANCY": TYPE_AMOUNT_DISCREPANCY,
    "RECONCILIATION_MISSING": TYPE_MISSING_ENTRY,
    "ORPHANED_SETTLEMENT": TYPE_UNKNOWN_ENTRY,
}


def _amt(amount: Optional[float], currency: Optional[str]) -> Optional[dict]:
    if amount is None:
        return None
    return {f"{pacs008.ATTRIBUTE_PREFIX}Ccy": currency or "USD", pacs008.TEXT_KEY: amount}


def _agent(bic: Optional[str]) -> Optional[dict]:
    return {"Agt": {"FinInstnId": {"BICFI": bic}}} if bic else None


def build(
    *,
    msg_id: str,
    case_id: str,
    exception: dict,
    payment: Optional[dict],
    note: Optional[str],
    now: datetime,
) -> dict:
    """The camt.110 for one exception. `payment` is None for an orphan statement line."""
    subject = exception.get("subjectRef") or {}
    detail = exception.get("detail") or {}
    currency = (payment or {}).get("currency")
    if subject.get("kind") == "STATEMENT_LINE":
        underlying = {"StmtNtry": {
            "OrgnlStmtId": subject.get("paymentMessageId"),
            "OrgnlNtryNb": subject.get("lineNo"),
            "OrgnlNtryRef": detail.get("reference"),
            "OrgnlAmt": _amt(detail.get("actualAmount"), currency),
        }}
    else:
        underlying = {"IntrBk": {
            "OrgnlUETR": pacs008.iso_uetr((payment or {}).get("uetr")),
            "OrgnlEndToEndId": exception.get("paymentId"),
            "OrgnlIntrBkSttlmAmt": _amt((payment or {}).get("amount"), currency),
        }}
    expected, booked = detail.get("expectedAmount"), detail.get("actualAmount")
    data = {
        "XpctdAmt": _amt(expected, currency),
        "BookdAmt": _amt(booked, currency),
        "AddtlInf": note or detail.get("discrepancyReason"),
    }
    # The responder is the bank that booked the line we dispute: our nostro's servicer, the
    # first hop out (`instructedAgentBic`, CHASUS33 for USD). The country correspondent is the
    # fallback for a wire with no such hop.
    correspondent = (payment or {}).get("correspondent") or {}
    correspondent_bic = correspondent.get("instructedAgentBic") or correspondent.get("correspondentBic")
    return {ROOT: {MESSAGE_ROOT: {
        "InvstgtnReq": {
            "MsgId": msg_id,
            "RqstrInvstgtnId": case_id,
            "CreDtTm": now,
            "InvstgtnTp": {"Prtry": _TYPE_BY_CATEGORY.get(exception.get("category"), "OTHR")},
            "Undrlyg": underlying,
            "Rqstr": _agent(bank_identity.OUR_BIC),
            "Rspndr": _agent(correspondent_bic),
        },
        "InvstgtnData": [{k: v for k, v in data.items() if v is not None}],
    }}}


def body(message: dict) -> dict:
    """The `InvstgtnReq` body, tolerating an already-unwrapped message."""
    return ((message or {}).get(ROOT) or {}).get(MESSAGE_ROOT) or message or {}


def to_xml(message: dict, *, indent: bool = True) -> str:
    """Serialise to camt.110 XML, reusing the pacs.008 mapper's `@attr`/`#text` walk."""
    from xml.etree import ElementTree as ET

    root = ET.Element(ROOT, {"xmlns": NAMESPACE})
    pacs008._append(root, MESSAGE_ROOT, body(message))
    if indent:
        ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
