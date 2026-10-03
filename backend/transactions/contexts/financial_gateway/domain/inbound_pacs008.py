"""Inbound pacs.008 -> canonical payment fields. Pure, and the exact inverse of `pacs008.py`.

Doina's inbound stage 1 (L363): *"external pacs.008 -> paymentMessages (direction: INBOUND)
-> Parse and validate the message -> Generate paymentId -> create `payments` document."*
This module is the "parse and validate" half; `application/receive.py` does the I/O.

## Read the structure from the mapper, never from memory

Every element path here is resolved through `pacs008`'s own constants — `pacs008.body()`,
`pacs008.TEXT_KEY`, `pacs008.ATTRIBUTE_PREFIX` — rather than repeating the literal keys.
Defect 2026-09-02 (`message-structure-unsourced`) is the reason: the outbound mapper shipped
three ISO-structure errors written from memory, and two of its consumers then read the old
shape after it was fixed because each had its own copy of the literals. A parser is just
another consumer. Going through the mapper's constants means a structure change cannot leave
this file reading a shape that no longer exists.

## What "validate" means here, and what it does not

Structural only: is this a pacs.008, does it carry exactly one credit transfer, are the
elements a payment cannot be built without present. **No business validation** — the account
may not exist, the name may not match, the originator may be sanctioned. Those are stages 2
and 3 (her L699: the inbound sequence is *reordered* relative to outgoing, not duplicated),
and they run against the persisted payment so a refusal is a traceable payment rather than a
dropped message.

The distinction matters for FR-1.IN1's guarantee: the raw message is stored **before** this
runs, so a message this rejects is still on disk to be replayed or inspected.
"""

from __future__ import annotations

from typing import Optional

from contexts.payment_rail.domain import pacs008

# The inbound rail is fixed by the channel the message arrived on, not chosen (her L385:
# "no payment-type selection UI — rail is fixed by the inbound channel").
INBOUND_RAIL = "WIRE"
INBOUND_TYPE = "CREDIT_TRANSFER"


class MessageRejected(ValueError):
    """The message is not a viable pacs.008 and no payment can be built from it.

    A `ValueError`, so the gateway route maps it to a 400 — but raised only AFTER the raw
    message is persisted (FR-1.IN1), so a rejection is never a lost message.
    """


def _text(node, default=None):
    """The text value of an element, whether it is a bare scalar or an `{@attr, #text}` dict.

    `IntrBkSttlmAmt` carries its currency as an XML attribute, so the amount arrives as
    `{"@Ccy": "USD", "#text": 25000.0}` while `ChrgBr` is a bare string. One accessor for
    both, keyed off the mapper's own convention constants.
    """
    if isinstance(node, dict):
        return node.get(pacs008.TEXT_KEY, default)
    return node if node is not None else default


def _attribute(node, name: str, default=None):
    if not isinstance(node, dict):
        return default
    return node.get(f"{pacs008.ATTRIBUTE_PREFIX}{name}", default)


def _agent_party(agent: Optional[dict]) -> dict:
    """`BranchAndFinancialInstitutionIdentification6` -> the bank half of a party snapshot."""
    fin = (agent or {}).get("FinInstnId") or {}
    member = fin.get("ClrSysMmbId") or {}
    return {
        "bic": fin.get("BICFI"),
        "bankName": fin.get("Nm"),
        "clearingSystemMemberId": member.get("MmbId"),
        "clearingSystemCode": (member.get("ClrSysId") or {}).get("Cd"),
    }


def _account_number(account: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
    """`CashAccount38` -> `(iban, accountNo)`. The inverse of `pacs008._account`."""
    identification = (account or {}).get("Id") or {}
    iban = identification.get("IBAN")
    other = (identification.get("Othr") or {}).get("Id")
    return iban, other


def _address(party: Optional[dict]) -> Optional[str]:
    """`PstlAdr/AdrLine` -> the flat string the payment snapshot stores.

    The outbound mapper writes a single address line because `creditor.address` is a
    formatted string (Q34); this reverses that, joining multiple lines if the sender sent
    them rather than silently keeping only the first.
    """
    lines = ((party or {}).get("PstlAdr") or {}).get("AdrLine") or []
    if isinstance(lines, str):
        return lines
    joined = ", ".join(line for line in lines if line)
    return joined or None


def parse(message: dict) -> dict:
    """The canonical fields an inbound pacs.008 yields. Raises `MessageRejected` if it cannot.

    Returns a flat dict the caller turns into a `PaymentContext` — deliberately not a
    `payments` document: `payment_document.build` remains the single place that decides the
    canonical shape, for inbound and outbound alike.
    """
    # ⚠️ `pacs008.body` is deliberately TOLERANT — it falls back to returning the message
    # itself when the envelope is absent, so an already-unwrapped body still reads. That is
    # right for a reader working on our own messages, and wrong here: an inbound message
    # comes from outside, so "is this even a pacs.008?" is a real question and the tolerant
    # fallback would answer it by accident. Check the envelope explicitly first, then use
    # `body()` for the extraction.
    envelope = (message or {}).get(pacs008.ROOT) or {}
    if pacs008.MESSAGE_ROOT not in envelope:
        raise MessageRejected(
            f"Not a pacs.008: no {pacs008.ROOT}/{pacs008.MESSAGE_ROOT} element."
        )
    body = pacs008.body(message)
    if not body:
        raise MessageRejected(f"Empty {pacs008.MESSAGE_ROOT} body — nothing to parse.")

    group_header = body.get("GrpHdr") or {}
    transactions = body.get("CdtTrfTxInf")
    if isinstance(transactions, dict):
        # 1..n in ISO, and the mapper always emits a list. A sender that emits a bare
        # object is still readable — tolerate it rather than rejecting a viable payment on
        # a shape technicality (be liberal in what you accept).
        transactions = [transactions]
    if not transactions:
        raise MessageRejected("pacs.008 carries no CdtTrfTxInf — nothing to apply.")
    if len(transactions) > 1:
        # Phase 1 applies one payment per message. A bulk message is a real ISO shape and a
        # real future feature; refusing it explicitly is honest, and silently applying only
        # the first transaction would lose money.
        raise MessageRejected(
            f"pacs.008 carries {len(transactions)} credit transfers; "
            "this gateway applies one payment per message."
        )

    transaction = transactions[0] or {}
    payment_identification = transaction.get("PmtId") or {}
    amount_node = transaction.get("IntrBkSttlmAmt")

    amount = _text(amount_node)
    currency = _attribute(amount_node, "Ccy")
    if amount is None:
        raise MessageRejected("pacs.008 has no IntrBkSttlmAmt — no amount to credit.")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise MessageRejected(f"IntrBkSttlmAmt {amount!r} is not a number.") from None
    if amount <= 0:
        raise MessageRejected(f"IntrBkSttlmAmt must be greater than 0, got {amount}.")
    if not currency:
        raise MessageRejected("IntrBkSttlmAmt carries no Ccy attribute.")

    creditor_iban, creditor_account_no = _account_number(transaction.get("CdtrAcct"))
    if not (creditor_iban or creditor_account_no):
        raise MessageRejected(
            "pacs.008 names no creditor account (CdtrAcct) — nothing to apply it to."
        )

    debtor_iban, debtor_account_no = _account_number(transaction.get("DbtrAcct"))
    debtor_party = transaction.get("Dbtr") or {}
    creditor_party = transaction.get("Cdtr") or {}
    debtor_agent = _agent_party(transaction.get("DbtrAgt") or group_header.get("InstgAgt"))
    creditor_agent = _agent_party(transaction.get("CdtrAgt") or group_header.get("InstdAgt"))
    remittance = transaction.get("RmtInf") or {}

    return {
        # --- identifiers, as the SENDER assigned them -------------------------
        # Kept under `sender*` names: these are the sending bank's identifiers and must not
        # be confused with the ones we mint. Our own `paymentId`/`endToEndId` are generated
        # locally exactly as they are outbound — a counterparty's reference never becomes
        # our primary key.
        "senderMsgId": group_header.get("MsgId"),
        "senderEndToEndId": payment_identification.get("EndToEndId"),
        "senderTxId": payment_identification.get("TxId"),
        "senderInstructionId": payment_identification.get("InstrId"),
        # The UETR is the exception and is deliberately carried through: it is the ISO
        # end-to-end tracking identifier, globally unique, and the whole point of it is that
        # it survives every hop. Re-minting it would break the tracking story.
        "uetr": payment_identification.get("UETR"),
        # --- the money --------------------------------------------------------
        "amount": amount,
        "currency": currency,
        "chargeBearer": _text(transaction.get("ChrgBr")),
        "settlementDate": group_header.get("IntrBkSttlmDt"),
        "createdDateTime": group_header.get("CreDtTm"),
        # --- the originator (external) ---------------------------------------
        "debtor": {
            "accountId": None,
            "accountNo": debtor_account_no,
            "iban": debtor_iban,
            "name": debtor_party.get("Nm"),
            "address": _address(debtor_party),
            "accountType": None,
            **debtor_agent,
        },
        # --- the claimed beneficiary (A.2) ------------------------------------
        # ⚠️ A CLAIM, not a confirmed identity. Her L387: the creditor here is "an account
        # number and name asserted by the sending bank", and confirming it against our own
        # records is stage 2's entire job (FR-2.IN3). Nothing downstream may treat these as
        # resolved until `beneficiaryResolution.matchOutcome` says so.
        "claimedCreditor": {
            "accountNo": creditor_account_no,
            "iban": creditor_iban,
            "name": creditor_party.get("Nm"),
            "address": _address(creditor_party),
            **creditor_agent,
        },
        # --- carried along for the ride --------------------------------------
        "purposeCode": (transaction.get("Purp") or {}).get("Cd"),
        "remittanceUnstructured": _first(remittance.get("Ustrd")),
        "remittanceReference": _structured_reference(remittance),
        "rail": INBOUND_RAIL,
        "type": INBOUND_TYPE,
    }


def _first(value):
    """`Ustrd` is 0..n; the canonical payment stores one unstructured string."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _structured_reference(remittance: dict) -> Optional[str]:
    structured = remittance.get("Strd")
    if isinstance(structured, list):
        structured = structured[0] if structured else None
    return ((structured or {}).get("CdtrRefInf") or {}).get("Ref")


def idempotency_key(parsed: dict) -> Optional[str]:
    """The inbound dedupe key (her L370: *"duplicate messages can be detected before
    creating duplicate payments"*).

    The UETR when the sender supplied one — it is globally unique by ISO definition and is
    the correct answer. Falling back to the sender's `(MsgId, TxId)` pair, which is unique
    per sending institution and the best available when the UETR is absent.

    Returns None when the message carries neither, rather than fabricating a key: a
    fabricated key would be unique every time and would silently disable dedupe, which is
    worse than having none and saying so.
    """
    if parsed.get("uetr"):
        return f"INBOUND-{parsed['uetr']}"
    msg_id = parsed.get("senderMsgId")
    txn_id = parsed.get("senderTxId") or parsed.get("senderEndToEndId")
    if msg_id and txn_id:
        return f"INBOUND-{msg_id}-{txn_id}"
    return None
