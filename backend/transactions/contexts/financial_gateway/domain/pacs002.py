"""Canonical payment -> ISO 20022 pacs.002.001.08 (FIToFIPaymentStatusReportV08). Pure.

The status report Leafy Bank sends back to the sending bank once an inbound payment is
accepted (FR-5.IN1). Her L938: inbound has nothing left to submit — *"execution" here means
generating and transmitting the status response back to the sending bank: a simulated
`pacs.002` confirming the payment will be applied*.

## ⚠️ Provenance: weaker than the pacs.008's, and stated rather than implied

`pacs008.py`'s structure was verified against a **local source** — the sibling
fsi-payments-processing demo's live format specification. **No such source exists for
pacs.002 or pacs.004**: that demo's `format_specifications.json` holds six formats and none
of them is a status or return message (verified by count, not by eye), and the local BIAN KG
indexes service-domain APIs rather than ISO message schemas. The ISO 20022 XSDs themselves
are behind registration.

So this structure comes from the published ISO message definition, and the honest statement
is: **the element names and nesting below are not machine-verified against a schema in this
workspace.** What IS enforced, by `test_stage_ten.py`:

  * the document envelope and root element exist (`Document/FIToFIPmtStsRpt`);
  * `TxInfAndSts` is a LIST (1..n in ISO), not a single object — the pacs.008's own
    2026-09-02 defect, not repeated;
  * no pacs.008-only element (`IntrBkSttlmAmt`, `ChrgBr`, `Dbtr`, `Cdtr`) appears here, and
    no pain.001-only element (`PmtMtd`) appears anywhere — the name-level check that the
    value-provenance guard is structurally blind to.

That last one is the lesson of defect 2026-09-02 applied up front: a message-format guard
must check element NAMES against the standard, not just where the values came from.

## Reuses the mapper's conventions, never its literals

`ROOT`, `TEXT_KEY` and `to_xml` come from `pacs008` — same XML-in-JSON convention, one
serialiser. A convention change there cannot leave this file emitting the old shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from contexts.payment_order_initiation.domain import bank_identity
from contexts.payment_rail.domain import pacs008

MESSAGE_FORMAT = "pacs.002.001.08"
MESSAGE_STANDARD = "ISO20022"
MAPPING_VERSION = "1.0.0"

ROOT = pacs008.ROOT
MESSAGE_ROOT = "FIToFIPmtStsRpt"
NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:pacs.002.001.08"

# ISO 20022 ExternalPaymentTransactionStatus1Code. `ACCP` is her L936's "status: ACCP or
# similar" — AcceptedCustomerProfile, the positive acknowledgement. `RJCT` is Rejected.
ACCEPTED = "ACCP"
REJECTED = "RJCT"

# Plain-English `AddtlInf` for the status-reason codes inbound can raise. ISO leaves the text
# to the sender of the report; a reader of the pacs.002 should not need the code list open.
REASON_TEXT = {
    "AC01": "Beneficiary account number is incorrect or unknown",
    "AC04": "Beneficiary account is closed",
    "AM05": "Duplicate payment",
    "RR04": "Sanctions screening hit on originator",
}


def build(
    *,
    payment: dict,
    accepted: bool,
    reason_code: Optional[str] = None,
    additional_info: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """The pacs.002 for one inbound payment. Pure — pass a payment dict, get a message.

    Every leaf traces to the payment document or to `bank_identity`; the only literals are
    ISO constants (`ACCP`/`RJCT`), the same discipline `pacs008.build` holds to.
    """
    sender = payment.get("senderReferences") or {}
    debtor = payment.get("debtor") or {}
    status = ACCEPTED if accepted else REJECTED

    group_header = {
        # OUR message id, not the sender's — this is a new message, and it is ours.
        "MsgId": _message_id(payment.get("paymentId"), now),
        "CreDtTm": now,
        # The instructing agent on a status report is the bank SENDING the report: us.
        "InstgAgt": _agent(bank_identity.OUR_BIC, bank_identity.OUR_BANK_NAME),
        # ...and the instructed agent is the bank that sent the original payment.
        "InstdAgt": _agent(debtor.get("bic"), debtor.get("bankName")),
    }

    # `OrgnlGrpInfAndSts` identifies the message being answered. The sender's own MsgId is
    # what they will match on — quoting our own here would make the report unmatchable.
    # No `OrgnlTxRef`: Doina's Oct 6 review wants it inside this block without `MsgRef`, which
    # was its only content, so an empty element is omitted. Our own link to the stored
    # pacs.008 lives on the paymentMessages document (`originalMessageRef`), not in the message.
    original_group = {
        "OrgnlMsgId": sender.get("msgId"),
        "OrgnlMsgNmId": pacs008.MESSAGE_FORMAT,
    }

    transaction_status = {
        # `OrgnlInstrId` / `OrgnlEndToEndId` / `OrgnlTxId` echo the SENDER's references, for
        # the same reason.
        "OrgnlInstrId": sender.get("instructionId"),
        "OrgnlEndToEndId": sender.get("endToEndId"),
        "OrgnlTxId": sender.get("txId"),
        # The UETR travels unchanged across every hop — the point of it.
        "OrgnlUETR": pacs008.iso_uetr(payment.get("uetr")),
        "TxSts": status,
        "StsRsnInf": _status_reason(reason_code, additional_info),
        # When the payment was accepted. Only on ACCP: a refusal was never accepted.
        "AccptncDtTm": now if accepted else None,
        "AcctSvcrRef": payment.get("paymentId"),
    }

    # `TxInfAndSts` is **1..n** in ISO — a status report can cover many transactions. We
    # always report one, but it stays a LIST, because a single object would be a different
    # document shape from every real pacs.002 and would break any consumer that iterates it.
    # (This is exactly the bug the pacs.008 shipped in 2026-09-02; not repeating it.)
    return {
        ROOT: {
            MESSAGE_ROOT: {
                "GrpHdr": group_header,
                "OrgnlGrpInfAndSts": original_group,
                "TxInfAndSts": [transaction_status],
            }
        }
    }


def _agent(bic: Optional[str], name: Optional[str] = None) -> Optional[dict]:
    """`BranchAndFinancialInstitutionIdentification6` — same shape as `pacs008._agent`.

    None when there is no agent at all: an absent optional element is correct ISO, an
    element present with null children is not.
    """
    if not any((bic, name)):
        return None
    fin = {}
    if bic:
        fin["BICFI"] = bic
    if name:
        fin["Nm"] = name
    return {"FinInstnId": fin}


def _message_id(payment_id: Optional[str], now: Optional[datetime]) -> str:
    """`STSRPT-{date}-{payment suffix}` — ours, unique per payment, readable at a glance."""
    day = now.strftime("%Y%m%d") if now else "00000000"
    suffix = str(payment_id or "").removeprefix("PAY-").upper() or "UNKNOWN"
    return f"STSRPT-{day}-{suffix}"


def _status_reason(reason_code: Optional[str], additional_info: Optional[str]) -> Optional[dict]:
    """`StsRsnInf` — present only on a refusal. An accepted payment needs no reason.

    `AddtlInf` sits inside `StsRsnInf` in ISO 20022, next to the coded reason it explains.
    """
    if not reason_code:
        return None
    info = {"Rsn": {"Cd": reason_code}}
    text = additional_info or REASON_TEXT.get(reason_code)
    if text:
        info["AddtlInf"] = text
    return info


def body(message: dict) -> dict:
    """The `FIToFIPmtStsRpt` body, tolerating an already-unwrapped message.

    One place that knows the envelope, so a reader never reaches through two literal keys —
    the device that stopped the pacs.008's envelope change breaking its consumers.
    """
    return ((message or {}).get(ROOT) or {}).get(MESSAGE_ROOT) or message or {}


def to_xml(message: dict, *, indent: bool = True) -> str:
    """Serialise to pacs.002 XML, reusing the mapper's mechanical walk.

    `pacs008._append` knows only the `@attr`/`#text` convention and nothing about pacs.008
    specifically, which is what makes it reusable here — one serialiser, one convention.
    """
    from xml.etree import ElementTree as ET

    root = ET.Element(ROOT, {"xmlns": NAMESPACE})
    pacs008._append(root, MESSAGE_ROOT, body(message))
    if indent:
        ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
