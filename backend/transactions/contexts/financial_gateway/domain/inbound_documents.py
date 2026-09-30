"""`paymentMessages` documents for the inbound flow. Pure — no I/O, no clock.

Three message documents the incoming wire produces, all in the collection stage 5 already
owns (`execution_documents.PAYMENT_MESSAGES`). No new collection: her L944 is explicit that
inbound *"uses the existing canonicalJsonStorage collection ... consistent with how it's
already used for outbound pacs.008 storage"* — and that collection is `paymentMessages`
under her own rename (L780, D-IN1).

| document        | direction | purpose           | requirement |
|-----------------|-----------|-------------------|-------------|
| received wire   | INBOUND   | `CREDIT_TRANSFER` | FR-1.IN1    |
| status response | OUTBOUND  | `STATUS_RESPONSE` | FR-5.IN2    |
| return          | OUTBOUND  | `RETURN`          | FR-9.IN3    |
| nostro statement| INBOUND   | `ACCOUNT_STATEMENT` | recon plan A1 (camt.053, no paymentId) |

⚠️ **`direction` here is the MESSAGE's travel, not the payment's.** An INBOUND payment emits
OUTBOUND pacs.002 and pacs.004 messages — two of the three rows above. The two fields are
orthogonal and neither may be derived from the other; conflating them is the
`discriminator-conflation` defect (2026-09-08) waiting to happen on a new axis.

Same discipline as `execution_documents.payment_message`: **a message is never a second
canonical payment** (her L887). No `status`, no `balance`, no `lifecycle` on any of these —
`test_no_inbound_message_is_a_second_canonical_payment` holds the line.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from bson import ObjectId

from contexts.payment_rail.domain import pacs008
from shared.refs import derive_ref

SOURCE_SYSTEM = "leafy-bank-payments-service"

INBOUND = "INBOUND"
OUTBOUND = "OUTBOUND"

# `purpose` distinguishes the three OUTBOUND artifacts a payment can produce, since
# `direction` alone no longer identifies a message once inbound exists: stage 5's outbound
# pacs.008 request, an inbound payment's pacs.002 acknowledgement and its pacs.004 return are
# all OUTBOUND. Her FR-5.IN2 and FR-9.IN3 name the last two values.
PURPOSE_CREDIT_TRANSFER = "CREDIT_TRANSFER"
PURPOSE_STATUS_RESPONSE = "STATUS_RESPONSE"
PURPOSE_RETURN = "RETURN"
# The correspondent's camt.053 statement for our nostro (reconciliation plan A1). Not tied to
# one payment — `paymentId` is None — so it is found by `statement.accountCode/window`.
PURPOSE_ACCOUNT_STATEMENT = "ACCOUNT_STATEMENT"

# The two response message types. pacs.002 is the status report (FR-5.IN1); pacs.004 is the
# payment return (FR-9.IN3). Versions match the pacs.008 generation the mapper emits.
STATUS_MESSAGE_FORMAT = "pacs.002.001.08"
RETURN_MESSAGE_FORMAT = "pacs.004.001.09"


def inbound_message_doc(
    *,
    oid: ObjectId,
    message: dict,
    now: datetime,
    payment_id: Optional[str] = None,
) -> dict:
    """The received pacs.008, stored verbatim (FR-1.IN1).

    `paymentId` is None at insert **by design** — the message is persisted before the
    payment is generated (her L363), and `receive._link_message` stamps it afterwards. That
    forward reference is the one permitted update on this collection.

    `rawMessage` carries the sender's document exactly as it arrived, unparsed and
    unmodified. This is the field that makes her five guarantees real (L368-372): replay,
    reconstruction, and an immutable audit record all need the ORIGINAL, not our reading of
    it. The outbound sibling has `rawMessageRef: None` because nothing is serialised there;
    here a real message exists, so we keep it.
    """
    return {
        "_id": oid,
        "paymentMessageId": derive_ref("PM", oid),
        # Null until the payment exists. Not an oversight — see the docstring.
        "paymentId": payment_id,
        # An inbound message has no execution attempt: `paymentExecutions` tracks OUTBOUND
        # rail submissions only, and her L944 keeps it that way for inbound.
        "paymentExecutionId": None,
        "direction": INBOUND,
        "purpose": PURPOSE_CREDIT_TRANSFER,
        "messageStandard": pacs008.MESSAGE_STANDARD,
        "messageFormat": pacs008.MESSAGE_FORMAT,
        "mappingVersion": pacs008.MAPPING_VERSION,
        # The sender's original. The whole point of persisting first.
        "rawMessage": message,
        "rawMessageRef": None,
        # No transformation audit: nothing was mapped OUT of a canonical payment here — the
        # payment was mapped IN from this. The parse is recorded on the payment's `checks[]`
        # where the rest of the inbound decision trail lives.
        "transformationAudit": None,
        "simulated": True,
        "sourceSystem": SOURCE_SYSTEM,
        "createdAt": now,
    }


def status_response_doc(
    *,
    oid: ObjectId,
    payment: dict,
    accepted: bool,
    reason_code: Optional[str],
    original_message_ref: Optional[str],
    now: datetime,
) -> dict:
    """The pacs.002 status report sent back to the sending bank (FR-5.IN1/2).

    ISO 20022 `FIToFIPaymentStatusReportV08`. `ACCP` (AcceptedCustomerProfile) is the
    positive status her L936 names; `RJCT` is its refusal counterpart, written when the
    acceptance decision was REJECT so the sender is told either way.

    The message body is built by `pacs002.build` — structure sourced from the standard's
    element names, not written here (defect 2026-09-02 `message-structure-unsourced`).
    """
    from contexts.financial_gateway.domain import pacs002

    message = pacs002.build(
        payment=payment,
        accepted=accepted,
        reason_code=reason_code,
        original_message_ref=original_message_ref,
        now=now,
    )
    return {
        "_id": oid,
        "paymentMessageId": derive_ref("PM", oid),
        "paymentId": payment.get("paymentId"),
        "paymentExecutionId": None,
        # ⚠️ OUTBOUND on an INBOUND payment — we are answering the sender. See the module
        # docstring: this field is the message's travel, never the payment's.
        "direction": OUTBOUND,
        "purpose": PURPOSE_STATUS_RESPONSE,
        "messageStandard": pacs008.MESSAGE_STANDARD,
        "messageFormat": STATUS_MESSAGE_FORMAT,
        "mappingVersion": pacs008.MAPPING_VERSION,
        "payload": message,
        "rawMessage": None,
        "rawMessageRef": None,
        # Which message this one answers — the inbound pacs.008's stored id.
        "originalMessageRef": original_message_ref,
        "statusCode": pacs002.ACCEPTED if accepted else pacs002.REJECTED,
        "reason": reason_code,
        "transformationAudit": None,
        "simulated": True,
        "sourceSystem": SOURCE_SYSTEM,
        "createdAt": now,
    }


def return_doc(
    *,
    oid: ObjectId,
    payment: dict,
    return_reason_code: str,
    original_message_ref: Optional[str],
    now: datetime,
) -> dict:
    """The pacs.004 PaymentReturn sent when an inbound payment cannot be applied (FR-9.IN3).

    Her L1350: a pacs.004 *"is a new outbound artifact type with no equivalent anywhere in
    the outgoing document — outgoing never generates a message that says 'we could not honor
    this'"*, because outbound's stage-3 gate stops an unviable payment before submission.
    Inbound cannot: the money has already arrived.
    """
    from contexts.financial_gateway.domain import pacs004

    message = pacs004.build(
        payment=payment,
        return_reason_code=return_reason_code,
        original_message_ref=original_message_ref,
        now=now,
    )
    return {
        "_id": oid,
        "paymentMessageId": derive_ref("PM", oid),
        "paymentId": payment.get("paymentId"),
        "paymentExecutionId": None,
        "direction": OUTBOUND,
        "purpose": PURPOSE_RETURN,
        "messageStandard": pacs008.MESSAGE_STANDARD,
        "messageFormat": RETURN_MESSAGE_FORMAT,
        "mappingVersion": pacs008.MAPPING_VERSION,
        "payload": message,
        "rawMessage": None,
        "rawMessageRef": None,
        "originalMessageRef": original_message_ref,
        "returnReasonCode": return_reason_code,
        "transformationAudit": None,
        "simulated": True,
        "sourceSystem": SOURCE_SYSTEM,
        "createdAt": now,
    }


def statement_doc(
    *,
    oid: ObjectId,
    message: dict,
    account_code: str,
    currency: str,
    window_from: datetime,
    window_to: datetime,
    sequence: int,
    opening_balance: float,
    closing_balance: float,
    entries: list[dict],
    now: datetime,
) -> dict:
    """The camt.053 statement received from the correspondent (plan-reconciliation-agent §A1).

    `payload` is the ISO message and is never mutated. `entries[]` is our flat projection of
    its `Ntry[]` — one row per line with a `recon{}` block — so statement matching (A2) can
    record its result without rewriting the correspondent's message.
    """
    from contexts.financial_gateway.domain import camt053

    return {
        "_id": oid,
        "paymentMessageId": derive_ref("PM", oid),
        "paymentId": None,
        "paymentExecutionId": None,
        "direction": INBOUND,
        "purpose": PURPOSE_ACCOUNT_STATEMENT,
        "messageStandard": camt053.MESSAGE_STANDARD,
        "messageFormat": camt053.MESSAGE_FORMAT,
        "mappingVersion": camt053.MAPPING_VERSION,
        "payload": message,
        "rawMessage": camt053.to_xml(message),
        "rawMessageRef": None,
        "statement": {
            "accountCode": account_code,
            "currency": currency,
            "window": {"from": window_from, "to": window_to},
            "sequence": sequence,
            "openingBalance": opening_balance,
            "closingBalance": closing_balance,
        },
        "entries": [{"lineNo": i + 1, **e} for i, e in enumerate(entries)],
        "transformationAudit": None,
        "simulated": True,
        "sourceSystem": SOURCE_SYSTEM,
        "createdAt": now,
    }
