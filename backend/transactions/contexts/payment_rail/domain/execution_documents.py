"""`paymentExecutions` and `paymentMessages` — the two documents stage 5 produces. Pure.

## Both collections are ours to author

`bian.py grep paymentExecution` returns **0 matches** across the 22 collections in
`Consolidated_..._v34_Aug17.json`, yet `payments.refs.paymentExecutionIds` names
`paymentExecutions` as an FK target with an explicit description. Same situation as stage 4's
two (doc 18 B1): the relationship was authored and the collection never was. Doina ratifies
both shapes — Q35, Q38.

`paymentMessages` is **her rename**, not our invention. Her target-model table reads
*"`canonicalJsonStorage/paymentMessages (renamed)`"* (L780) and her *Existing collections*
list carries *"`canonicalJsonStorage?`"* with her own question mark (L734).
⚠️ **`canonicalJsonStorage` is a different demo's live collection** — 33-34 documents on
`ist-shared.leafy_bank_bian`, owned by fsi-payments-processing, holding a flat
`MT103_to_JSON` conversion-run shape. Nothing in this backend reads, writes or indexes it,
and `test_nothing_in_the_backend_touches_canonical_json_storage` keeps it that way (doc 19 B2).

## The split, and why the two are separate documents

Her L887-892 draws the line: *"`payments` = canonical business payment; [the message
collection] = canonicalized message/protocol representation"*, used for *"inbound or outbound
message conversion, mapping versions, raw-message references, and transformation audit"*.
`paymentExecutions` (her L854) is a different thing: *"one document for each execution attempt
or execution path"*.

The distinction earns its keep on the **inbound** side. Her L899-906 sequence is
`external message -> [paymentMessages] -> payments -> ... -> paymentExecutions`, so an
inbound message exists *before* any payment and has no execution attempt at all. Collapsing
the payload onto the execution document would leave it nowhere to live. `direction` is
therefore named now and only `OUTBOUND` is reachable in Phase 1 (doc 19 §6).

## Append-only, and exactly what that means

Doina: *"Repairs, recalls, returns, and retries should create additional execution artifacts
rather than overwrite the original."* That is about **attempts**: a retry inserts
`attempt: n+1` and never touches attempt 1. A rail acknowledgement is not a new attempt — it
is *this* attempt's outcome — so `acknowledge()` below produces the one `$set` that is
permitted, over three named fields. Nothing else ever updates either collection, and
`test_nothing_replaces_or_deletes_a_payment_execution` walks the AST for it rather than
trusting this paragraph.

## Both ids are minted before either document is built

`derive_ref` is deterministic from the ObjectId, so the caller mints both oids up front and
each document carries the other's ref **at insert**. Her L892 asks for `paymentExecutionId` on
the message; doing it this way means no forward-reference `$set` and no update path on
`paymentMessages` at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from bson import ObjectId

from contexts.payment_rail.domain import pacs008
from shared.refs import derive_ref

PAYMENT_EXECUTIONS = "paymentExecutions"
PAYMENT_MESSAGES = "paymentMessages"

SOURCE_SYSTEM = "leafy-bank-payments-service"

OUTBOUND = "OUTBOUND"
INBOUND = "INBOUND"

# `paymentExecutions.status` — the *internal* normalised status. Her L255-258 asks for both
# this and the rail's own status, which is why `railStatus{}` is a separate block.
SUBMITTED = "SUBMITTED"
ACKNOWLEDGED = "ACKNOWLEDGED"
FAILED = "FAILED"


def payment_execution(
    *,
    payment: dict,
    strategy: Any,
    message: dict,
    payment_message_id: Optional[str],
    attempt: int = 1,
    now: datetime,
    oid: Optional[ObjectId] = None,
) -> dict:
    """One execution **attempt** against a rail, with the message that was sent.

    Her L854-862: one document per attempt, and *"this is where the canonical payment is
    transformed into a rail-specific message"* — so the message travels on the artifact that
    records the attempt, not on the payment.

    `status` starts at `SUBMITTED` because that is true at insert: the message has been
    handed to the rail and no answer has arrived. `acknowledge()` moves it.
    """
    oid = oid or ObjectId()
    refs = payment.get("refs") or {}
    return {
        "_id": oid,
        "paymentExecutionId": derive_ref("PE", oid),
        "paymentId": payment.get("paymentId"),
        "attempt": attempt,
        # --- what was executed, and how -------------------------------------
        "rail": payment.get("rail"),
        "clearingNetwork": getattr(strategy, "network", None),
        "executionStrategy": getattr(strategy, "strategy", None),
        "valueDate": getattr(strategy, "value_date", None),
        "amount": payment.get("amount"),
        "currency": payment.get("currency"),
        # --- the message, as sent -------------------------------------------
        "messageStandard": pacs008.MESSAGE_STANDARD,
        "messageFormat": pacs008.MESSAGE_FORMAT,
        "message": message,
        # --- the decisions this attempt rests on -----------------------------
        "paymentMessageId": payment_message_id,
        "routingSnapshotId": refs.get("routingSnapshotId"),
        "paymentOrderId": refs.get("paymentOrderId"),
        # --- status: ours, and the rail's (her L255-258) ---------------------
        "status": SUBMITTED,
        "railStatus": {"code": None, "reason": None, "messageRef": None},
        "submittedAt": now,
        "acknowledgedAt": None,
        # --- provenance -------------------------------------------------------
        # SIMULATED and the document says so: *"the demo will not connect to a real payment
        # network"*. Same discipline as the routing snapshot's correspondent block.
        "simulated": True,
        "sourceSystem": SOURCE_SYSTEM,
        "createdAt": now,
    }


def acknowledge(ack: Any, *, now: datetime) -> dict:
    """The one permitted `$set` on an execution attempt: the rail's answer to it.

    Three fields, named here so the AST guard can assert nothing else is ever updated. A
    repair or retry does **not** come through here — it inserts a new attempt.
    """
    return {
        "status": ACKNOWLEDGED if ack.accepted else FAILED,
        "railStatus": {
            "code": ack.status_code,
            "reason": ack.reason,
            "messageRef": ack.message_ref,
        },
        "acknowledgedAt": now,
    }


def payment_message(
    *,
    payment: dict,
    message: dict,
    payment_execution_id: Optional[str],
    settlement_mtd: Optional[str],
    now: datetime,
    direction: str = OUTBOUND,
    oid: Optional[ObjectId] = None,
) -> dict:
    """The canonicalized message/protocol representation (her L887-892).

    ⚠️ **This document is not a second canonical payment.** Her L887 is explicit: *"Do not
    allow both `payments` and [this collection] to be authoritative canonical payment
    collections."* So it carries no `status`, no balance and no lifecycle — only the payload
    that was mapped, what it was mapped to, and the audit of the mapping.
    `test_the_payment_message_is_not_a_second_canonical_payment` holds the line.

    `payload` is the **normalised canonical payload the mapper consumed** — the projection of
    `payments` that the ISO document was built from. It is what makes the demo's BUSINESS
    VIEW / ISO VIEW pair renderable side by side from one document.
    """
    oid = oid or ObjectId()
    return {
        "_id": oid,
        "paymentMessageId": derive_ref("PM", oid),
        "paymentId": payment.get("paymentId"),
        # Her L892: *"shall include `paymentId` and, where applicable, `paymentExecutionId`
        # so it is traceable to the lifecycle."* Null on an inbound message, which has no
        # execution attempt.
        "paymentExecutionId": payment_execution_id,
        "direction": direction,
        # --- what it was mapped to -------------------------------------------
        "messageStandard": pacs008.MESSAGE_STANDARD,
        "messageFormat": pacs008.MESSAGE_FORMAT,
        "mappingVersion": pacs008.MAPPING_VERSION,
        # --- the payload the mapper consumed ---------------------------------
        "payload": canonical_payload(payment),
        # No raw wire message exists: the rail is simulated and nothing is serialised to
        # SWIFT/Fedwire syntax. Null rather than a fabricated reference (her *"raw-message
        # references"* is a real requirement the moment a real gateway exists).
        "rawMessageRef": None,
        # --- transformation audit (her L892) ---------------------------------
        "transformationAudit": transformation_audit(
            message, settlement_mtd=settlement_mtd, now=now
        ),
        "simulated": True,
        "sourceSystem": SOURCE_SYSTEM,
        "createdAt": now,
    }


def canonical_payload(payment: dict) -> dict:
    """The normalised canonical payload — the BUSINESS VIEW half of her two tabs.

    Her L550-553 names three lines (payment id, `debtor -> creditor`, amount); the rest is
    what a reader needs to check the ISO view against. Copied by value from `payments`, and
    deliberately **flat**: this is the projection the mapper consumed, not a second envelope.
    """
    debtor = payment.get("debtor") or {}
    creditor = payment.get("creditor") or {}
    remittance = payment.get("remittance") or {}
    wire = payment.get("wireDetails") or {}
    return {
        "paymentId": payment.get("paymentId"),
        "endToEndId": payment.get("endToEndId"),
        "uetr": payment.get("uetr"),
        "type": payment.get("type"),
        "rail": payment.get("rail"),
        "clearingNetwork": wire.get("network"),
        "amount": payment.get("amount"),
        "currency": payment.get("currency"),
        "chargeBearer": payment.get("chargeBearer"),
        "requestedExecutionDate": payment.get("requestedExecutionDate"),
        "debtorName": debtor.get("name"),
        "debtorAccountNo": debtor.get("accountNo"),
        "debtorBic": debtor.get("bic"),
        "creditorName": creditor.get("name"),
        "creditorAccountNo": creditor.get("accountNo"),
        "creditorBic": creditor.get("bic"),
        "creditorBankName": creditor.get("bankName"),
        "creditorBankCountry": creditor.get("bankCountry"),
        "purposeCode": remittance.get("purposeCode"),
        "remittanceInfo": remittance.get("unstructured"),
    }


def transformation_audit(message: dict, *, settlement_mtd: Optional[str],
                         now: datetime) -> list:
    """One entry per produced element group, plus the one value we derived.

    The derived entry matters: `SttlmMtd` is the single element in the message with no home in
    the canonical model (Q40), so the audit records that we derived it rather than read it.
    Everything else is a projection, and saying so is what makes the claim checkable.
    """
    entries = [
        {
            "element": path,
            "source": "PROJECTION",
            "detail": "Mapped by value from the canonical payment.",
            "at": now,
        }
        for path in pacs008.elements(message)
    ]
    entries.append({
        "element": "GrpHdr/SttlmInf/SttlmMtd",
        "source": "DERIVED",
        "detail": (
            f"{settlement_mtd} derived from the stage-4 routing decision — no settlement "
            "method is modelled anywhere in the canonical spec (Q40)."
        ),
        "at": now,
    })
    return entries
