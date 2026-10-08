"""Canonical payment -> ISO 20022 pacs.004.001.09 (PaymentReturnV09). Pure.

The message Leafy Bank sends when an inbound payment cannot be applied and the funds go back
to the sender (FR-9.IN3). Her L1350 on why this has no outbound twin:

    *"A pacs.004 is a new outbound artifact type with no equivalent anywhere in the outgoing
    document — outgoing never generates a message that says 'we could not honor this',
    because outgoing's Stage 3 validation gate prevents an unviable payment from ever being
    submitted in the first place. Inbound cannot apply that same prevention, because the
    payment has already arrived before Leafy Bank can evaluate it."*

## ⚠️ Same provenance caveat as pacs002.py

No local ISO source exists for this message in the workspace (checked: the sibling demo's
format spec carries six formats, none a return message). Structure is from the published ISO
message definition and is **not machine-verified against a schema here**. What is enforced by
`test_stage_ten.py` is the same three-part name-level check: envelope + root, `TxInf` is a
LIST (1..n), and no pacs.008-only or pain.001-only element appears.

## The returned amount is its own element

`RtrdIntrBkSttlmAmt` — *returned* interbank settlement amount — is not `IntrBkSttlmAmt`. It
is a distinct element precisely because a return may be partial (fees deducted en route), and
using the pacs.008 name here would be the "neighbouring message type" error defect 2026-09-02
names. This demo always returns the full amount; the element is still the correct one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from contexts.payment_order_initiation.domain import bank_identity
from contexts.payment_rail.domain import pacs008
from contexts.financial_gateway.domain import pacs002

MESSAGE_FORMAT = "pacs.004.001.09"
MESSAGE_STANDARD = "ISO20022"
MAPPING_VERSION = "1.0.0"

ROOT = pacs008.ROOT
MESSAGE_ROOT = "PmtRtr"
NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:pacs.004.001.09"

_NB_OF_TXS = "1"

# ISO 20022 ExternalReturnReason1Code — the subset this demo can produce.
#   AC04 ClosedAccountNumber      — the beneficiary account is closed
#   AC01 IncorrectAccountNumber   — no account matches what the sender named
#   MS03 NoReasonSpecified        — an operator returned it without a coded reason
#   RR04 RegulatoryReason         — refused on screening
REASON_ACCOUNT_CLOSED = "AC04"
REASON_ACCOUNT_INCORRECT = "AC01"
REASON_UNSPECIFIED = "MS03"
REASON_REGULATORY = "RR04"

RETURN_REASON_CODES = frozenset({
    REASON_ACCOUNT_CLOSED,
    REASON_ACCOUNT_INCORRECT,
    REASON_UNSPECIFIED,
    REASON_REGULATORY,
})


def build(
    *,
    payment: dict,
    return_reason_code: str,
    original_message_ref: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """The pacs.004 for one returned inbound payment. Pure.

    The parties are **swapped** relative to the original pacs.008, and that is the whole
    semantic of a return: the bank that received the funds is now the one sending them, so
    the original creditor agent (us) becomes the debtor agent of the return.
    """
    sender = payment.get("senderReferences") or {}
    debtor = payment.get("debtor") or {}
    creditor = payment.get("creditor") or {}

    group_header = {
        "MsgId": f"RTN-{payment.get('paymentId')}",
        "CreDtTm": now,
        "NbOfTxs": _NB_OF_TXS,
        "SttlmInf": {"SttlmMtd": pacs008.SETTLEMENT_CLEARING},
        # We are returning the funds, so we are the instructing agent.
        "InstgAgt": pacs002._agent(bank_identity.OUR_BIC, bank_identity.OUR_BANK_NAME),
        "InstdAgt": pacs002._agent(debtor.get("bic"), debtor.get("bankName")),
    }

    transaction = {
        # OUR reference for the return itself, distinct from the original's.
        "RtrId": f"RTN-{payment.get('paymentId')}",
        # The sender's original references, so they can match the return to the payment.
        "OrgnlGrpInf": {
            "OrgnlMsgId": sender.get("msgId"),
            "OrgnlMsgNmId": pacs008.MESSAGE_FORMAT,
        },
        "OrgnlEndToEndId": sender.get("endToEndId"),
        "OrgnlTxId": sender.get("txId"),
        "OrgnlUETR": pacs008.iso_uetr(payment.get("uetr")),
        # ⚠️ `RtrdIntrBkSttlmAmt`, not `IntrBkSttlmAmt` — see the module docstring.
        "RtrdIntrBkSttlmAmt": {
            f"{pacs008.ATTRIBUTE_PREFIX}Ccy": payment.get("instructedCurrency")
            or payment.get("currency"),
            pacs008.TEXT_KEY: payment.get("instructedAmount") or payment.get("amount"),
        },
        "RtrRsnInf": {"Rsn": {"Cd": return_reason_code}},
        # The parties of the RETURN leg: we send, the original originator receives.
        "RtrChain": {
            "Dbtr": {"Pty": {"Nm": creditor.get("name")}},
            "DbtrAgt": pacs002._agent(bank_identity.OUR_BIC, bank_identity.OUR_BANK_NAME),
            "Cdtr": {"Pty": {"Nm": debtor.get("name")}},
            "CdtrAgt": pacs002._agent(debtor.get("bic"), debtor.get("bankName")),
        },
        "OrgnlTxRef": {"MsgRef": original_message_ref} if original_message_ref else None,
    }

    # `TxInf` is 1..n in ISO — a LIST, for the same reason as the pacs.008's `CdtTrfTxInf`
    # and the pacs.002's `TxInfAndSts`.
    return {ROOT: {MESSAGE_ROOT: {"GrpHdr": group_header, "TxInf": [transaction]}}}


def body(message: dict) -> dict:
    """The `PmtRtr` body, tolerating an already-unwrapped message."""
    return ((message or {}).get(ROOT) or {}).get(MESSAGE_ROOT) or message or {}


def to_xml(message: dict, *, indent: bool = True) -> str:
    """Serialise to pacs.004 XML, reusing the mapper's mechanical walk."""
    from xml.etree import ElementTree as ET

    root = ET.Element(ROOT, {"xmlns": NAMESPACE})
    pacs008._append(root, MESSAGE_ROOT, body(message))
    if indent:
        ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
