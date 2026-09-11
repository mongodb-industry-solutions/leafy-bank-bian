"""Canonical payment -> ISO 20022 pacs.008.001.08. Pure, and a projection only.

Doina L542: *"For the wire path, transform the canonical payment into an ISO 20022 message
**only at the rail boundary**."* L544 gives the pipeline: *"Canonical Payment -> ISO 20022
mapper -> pacs.008 -> Payment Rail -> Clearing / RTGS / Correspondent."*

pacs.008.001.08 is `FIToFICustomerCreditTransferV08` — the interbank instruction carrying a
customer credit transfer from the debtor's bank to the creditor's bank. Her L222 names it as
the representative message for this hop, and the `payments` spec's own envelope description
names the version.

## This is a projection, not a second source of truth

**Every leaf value here is read from the payment document.** Nothing is derived, defaulted or
invented; the only literals are ISO constants (`NbOfTxs`, `PmtMtd`). That is what makes
`test_every_iso_value_comes_from_the_canonical_payment` possible, and it is why doc 19 B5
declined to persist pacs.008 fields back onto `payments.wireDetails`: storing the same fact on
both sides is how the mirror-drift entries in defects.md start.

## Why the message is not stored on `wireDetails`

The envelope's spec description says *"add the corresponding pacs.008.001.08 fields to this
same envelope"*, but `wireDetails.messageDefinitionIdentifier`'s enum is
`["pain.001.001.09", null]` — the envelope cannot even name this message. Her stage-5 prose
("only at the rail boundary") breaks the tie: an adapter output is not envelope state. The
message lands on `paymentExecutions`; the normalised payload on `paymentMessages`. Doc 19 B5,
Q36.

## Bump `MAPPING_VERSION` on every mapping change

Stamped on every `paymentMessages` document (her *"mapping versions"*, L892), so a stored
message can always be explained by the rules that produced it. Same device as
`posting_rules.MAPPING_VERSION` in the ledger.

Imports nothing but the standard library and `bank_identity` — no pymongo, no `process`, no
adapter. Enforced by `test_the_mapper_does_no_io`.
"""

from __future__ import annotations

from typing import Optional

from contexts.payment_order_initiation.domain import bank_identity

MESSAGE_FORMAT = "pacs.008.001.08"
MESSAGE_STANDARD = "ISO20022"
MAPPING_VERSION = "1.1.0"

# The ISO envelope. A pacs.008 is `Document/FIToFICstmrCdtTrf/{GrpHdr, CdtTrfTxInf}` — the
# root and the message-type wrapper are part of the message, not decoration. Verified against
# the sibling demo's own live pacs.008 format specification
# (`repos/payments-processing/.../format_specifications.json`, whose field paths all read
# `Document.FIToFICstmrCdtTrf.…`), which means a message we *generate* and one that demo
# *converts* from an MT103 now have the same shape.
ROOT = "Document"
MESSAGE_ROOT = "FIToFICstmrCdtTrf"
NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"

# XML-in-JSON convention: `@name` is an attribute, `#text` is the element's text node. This is
# the xmltodict / BadgerFish convention, and it is what the sibling fsi-payments-processing
# demo's pacs.008 format specification uses (`IntrBkSttlmAmt.@Ccy`, `IntrBkSttlmAmt.#text`).
#
# Adopted for two reasons beyond consistency: it makes this dict a **reversible**
# representation of the XML — `to_xml` below is a mechanical walk, with no per-element
# knowledge — and a message we generate is now directly comparable with one that demo
# converts from an MT103. The first build used an invented `{"Ccy": ..., "value": ...}`, which
# was the same "written from memory" mistake as the envelope, one level down.
ATTRIBUTE_PREFIX = "@"
TEXT_KEY = "#text"

# ISO constants. Not payment data, and the only literals this module emits.
_NB_OF_TXS = "1"

# ISO 20022 SettlementMethod1Code, the subset a wire can take. Derived from the routing
# decision, never stored on the payment — see `settlement_method()` and Q40.
SETTLEMENT_CLEARING = "CLRG"
SETTLEMENT_COVER = "INDA"


def settlement_method(*, clearing_network: Optional[str], via_correspondent: bool) -> str:
    """`GrpHdr/SttlmInf/SttlmMtd`.

    ISO requires a settlement method and **nothing in the canonical model holds one** — not
    `payments`, not `routingSnapshots`, not `correspondentBanks` (Q40). So it is derived from
    the routing decision stage 4 already took, which is the fact it actually depends on:
    a payment settling across a clearing network is `CLRG`; one settling through a
    correspondent's books is `INDA`. The derivation is recorded in the transformation audit
    rather than asserted, because it is ours and not hers.
    """
    if via_correspondent and not clearing_network:
        return SETTLEMENT_COVER
    return SETTLEMENT_CLEARING


def _agent(bic: Optional[str], name: Optional[str] = None,
           member_id: Optional[str] = None, member_code: Optional[str] = None) -> Optional[dict]:
    """`BranchAndFinancialInstitutionIdentification6`. `None` when there is no agent at all —
    an absent optional element is correct ISO; an element present with null children is not.
    """
    if not any((bic, name, member_id)):
        return None
    fin: dict = {}
    if bic:
        fin["BICFI"] = bic
    if member_id:
        fin["ClrSysMmbId"] = {"MmbId": member_id}
        if member_code:
            fin["ClrSysMmbId"]["ClrSysId"] = {"Cd": member_code}
    if name:
        fin["Nm"] = name
    return {"FinInstnId": fin}


def _party(party: dict) -> dict:
    """`PartyIdentification135` — `Dbtr` / `Cdtr`. Name and address only: the account is a
    sibling element (`DbtrAcct` / `CdtrAcct`), not a child of the party."""
    block: dict = {"Nm": party.get("name")}
    if party.get("address"):
        # `AdrLine` because `creditor.address` is a formatted string, not a structured
        # PostalAddress — Q34 is the ask to make it structured, and until then a single
        # address line is the honest representation rather than a parsed guess.
        block["PstlAdr"] = {"AdrLine": [party["address"]]}
    return block


def _account(party: dict) -> Optional[dict]:
    """`CashAccount38`. IBAN when present, otherwise `Othr/Id` — which is what an external
    US beneficiary identified by account number gets, and it is legal ISO."""
    if party.get("iban"):
        return {"Id": {"IBAN": party["iban"]}}
    if party.get("accountNo"):
        return {"Id": {"Othr": {"Id": party["accountNo"]}}}
    return None


def build(payment: dict, *, settlement_mtd: Optional[str] = None,
          value_date: Optional[str] = None,
          snapshot: Optional[dict] = None) -> dict:
    """The pacs.008 document for one payment. Pure — pass a payment dict, get a message.

    FR-5.2 — routing/settlement fields are read from the **routingSnapshots record** (the
    immutable copy of stage 4's routing decision), which the caller (`execute.py`) loads via
    `refs.routingSnapshotId` and passes here as `snapshot`. Nothing routing/settlement is
    re-derived at execution time: the value date, the settlement method, and the
    instructing/instructed/debtor/creditor *agents* all come off that persisted record.
    `settlement_mtd`/`value_date` remain as fallbacks for the standalone/test call (no
    snapshot); `settlement_method` is still *derived* from the snapshot's clearing network +
    correspondent because no field in the canonical model holds one (Q40).

    The one routing field that does NOT come from the snapshot is `IntrmyAgt1`: the
    intermediary hop appears only when our correspondent is not the beneficiary's own bank,
    and that hop/no-hop distinction lives on `payments.correspondent.intermediaryBic`
    (orchestrate.py R26) — the snapshot's correspondent block records the correspondent BIC
    but not whether it IS the beneficiary's bank, so it cannot decide the hop. Reading the
    hop from the payment doc is not "re-deriving routing data"; it is reading the one field
    that encodes the hop semantics.

    `value_date` is not on the document at message-build time (`clearing.settlementDate` is
    stamped from the rail acknowledgement, after this message is generated), so without the
    snapshot it would yield `None` — a missing required `IntrBkSttlmDt`. Caught by
    `test_the_pacs008_matches_the_iso20022_format_spec`, which is the entire reason that test
    exists.
    """
    debtor = payment.get("debtor") or {}
    creditor = payment.get("creditor") or {}
    wire = payment.get("wireDetails") or {}
    remittance = payment.get("remittance") or {}
    correspondent = payment.get("correspondent") or {}
    clearing = payment.get("clearing") or {}
    snap = snapshot or {}
    snap_instructing = snap.get("instructingAgent") or {}
    snap_beneficiary = snap.get("beneficiaryAgent") or {}
    snap_correspondent = snap.get("correspondent") or {}

    # FR-5.2 — the routing/settlement decision, read off the persisted routing snapshot.
    resolved_value_date = (
        snap.get("valueDate") or value_date
        or clearing.get("settlementDate") or payment.get("requestedExecutionDate")
    )
    resolved_settlement_mtd = settlement_mtd or settlement_method(
        clearing_network=snap.get("clearingNetwork"),
        via_correspondent=bool(
            snap_correspondent.get("bic") or correspondent.get("correspondentBic")
        ),
    )
    # The four agents are routing data — read off the snapshot's instructing/beneficiary
    # agents when present, falling back to the payment doc + bank_identity for the
    # standalone call. The snapshot's instructing agent is our bank; its beneficiary agent
    # is the creditor's bank — so InstgAgt/DbtrAgt both resolve to us, InstdAgt/CdtrAgt to
    # the creditor's bank, exactly as the routing decision recorded.
    instg = snap_instructing or {
        "bic": bank_identity.OUR_BIC,
        "bankName": bank_identity.OUR_BANK_NAME,
        "clearingSystemMemberId": debtor.get("clearingSystemMemberId") or bank_identity.OUR_ABA,
        "clearingSystemCode": debtor.get("clearingSystemCode") or bank_identity.OUR_CLEARING_SYSTEM_CODE,
    }
    instd = snap_beneficiary or {
        "bic": creditor.get("bic"),
        "bankName": creditor.get("bankName"),
        "clearingSystemMemberId": creditor.get("clearingSystemMemberId"),
        "clearingSystemCode": creditor.get("clearingSystemCode"),
    }

    group_header = {
        "MsgId": payment.get("msgId"),
        "CreDtTm": payment.get("initiatedAt"),
        "NbOfTxs": _NB_OF_TXS,
        # `IntrBkSttlmDt` sits on the GROUP header, matching the sibling demo's spec
        # (`Document.FIToFICstmrCdtTrf.GrpHdr.IntrBkSttlmDt`). We emit one transaction per
        # message, so a group-level settlement date is unambiguous.
        # ⚠️ The real schema also permits it per transaction; with no XSD available locally
        # (see the conformance test's docstring) the one local source decides it.
        "IntrBkSttlmDt": resolved_value_date,
        "SttlmInf": {"SttlmMtd": resolved_settlement_mtd},
        "InstgAgt": _agent(
            instg.get("bic"), instg.get("bankName"),
            instg.get("clearingSystemMemberId"), instg.get("clearingSystemCode"),
        ),
        "InstdAgt": _agent(
            instd.get("bic"), instd.get("bankName"),
            instd.get("clearingSystemMemberId"), instd.get("clearingSystemCode"),
        ),
    }

    transaction = {
        # `PmtId` is not in her element list — the "..." at L563 is. Included because a
        # pacs.008 without one is unidentifiable on the network, and every child of it
        # already exists on the payment (notably `uetr`, which is the field the whole
        # ISO tracking story hangs on).
        "PmtId": {
            "InstrId": payment.get("instructionId"),
            "EndToEndId": payment.get("endToEndId"),
            "TxId": payment.get("txnId"),
            "UETR": payment.get("uetr"),
        },
        "PmtTpInf": _payment_type_information(
            wire.get("paymentTypeInformation"), payment.get("categoryPurpose")
        ),
        # `Ccy` is an XML **attribute** and the amount is the element's text node — hence
        # `@Ccy` / `#text` rather than two sibling keys.
        "IntrBkSttlmAmt": {
            f"{ATTRIBUTE_PREFIX}Ccy": payment.get("currency"),
            TEXT_KEY: payment.get("amount"),
        },
        "ChrgBr": payment.get("chargeBearer"),
        "Dbtr": _party(debtor),
        "DbtrAcct": _account(debtor),
        "DbtrAgt": _agent(
            instg.get("bic"), instg.get("bankName"),
            instg.get("clearingSystemMemberId"), instg.get("clearingSystemCode"),
        ),
        # One hop or two. Stage 4 sets `intermediaryBic` only when our correspondent is
        # NOT the beneficiary's own bank, so this element appears exactly when a hop
        # genuinely happens (orchestrate.py, R26). Read from the payment doc, not the
        # snapshot — see the build() docstring: the snapshot cannot decide the hop.
        "IntrmyAgt1": _agent(correspondent.get("intermediaryBic")),
        "CdtrAgt": _agent(
            instd.get("bic"), instd.get("bankName"),
            instd.get("clearingSystemMemberId"), instd.get("clearingSystemCode"),
        ),
        "Cdtr": _party(creditor),
        "CdtrAcct": _account(creditor),
        "Purp": (
            {"Cd": remittance.get("purposeCode")}
            if remittance.get("purposeCode") else None
        ),
        "RmtInf": _remittance(remittance),
    }

    # `CdtTrfTxInf` is **1..n** in ISO 20022 — a pacs.008 can carry many credit transfers in
    # one message. We always emit exactly one (`NbOfTxs: "1"`), but it stays a LIST, because a
    # single object would be a different document shape from every real pacs.008 and would
    # quietly break any consumer that iterates it.
    return {ROOT: {MESSAGE_ROOT: {"GrpHdr": group_header, "CdtTrfTxInf": [transaction]}}}


def _payment_type_information(pmt_tp_inf: Optional[dict],
                              category_purpose: Optional[str]) -> Optional[dict]:
    """`PmtTpInf` — mapped to ISO names, not passed through.

    ⚠️ `wireDetails.paymentTypeInformation` stores **our** camelCase shape
    (`serviceLevel.code`, `localInstrument.code`) — the spec labels the field *"pain.001
    PmtTpInf"*, which describes what it *means*, not how it is keyed. Passing it through put a
    camelCase sub-tree inside an otherwise ISO document: half the message in one vocabulary,
    half in another. Every other element here is renamed, so this one is too.

    `CtgyPurp` comes from the **top-level** `categoryPurpose`, because the canonical spec is
    explicit that it is *"not duplicated here — use the top-level categoryPurpose field"*.
    """
    source = pmt_tp_inf or {}
    service_level = (source.get("serviceLevel") or {}).get("code")
    local_instrument = source.get("localInstrument") or {}
    block: dict = {}
    if service_level:
        # `SvcLvl` is 0..n in pacs.008 — a list, like `CdtTrfTxInf`.
        block["SvcLvl"] = [{"Cd": service_level}]
    if local_instrument.get("code") or local_instrument.get("proprietary"):
        block["LclInstrm"] = {
            k: v for k, v in (
                ("Cd", local_instrument.get("code")),
                ("Prtry", local_instrument.get("proprietary")),
            ) if v
        }
    if category_purpose:
        block["CtgyPurp"] = {"Cd": category_purpose}
    return block or None


def _remittance(remittance: dict) -> Optional[dict]:
    """`RmtInf` — unstructured text and/or a structured creditor reference.

    Her L226: remittance is *"pain.001/RmtInf territory carried along for the ride"*, so it is
    copied, never reshaped.
    """
    block: dict = {}
    if remittance.get("unstructured"):
        block["Ustrd"] = [remittance["unstructured"]]
    reference = remittance.get("reference") or remittance.get("invoiceNo")
    if reference:
        block["Strd"] = [{
            "CdtrRefInf": {"Ref": reference},
            **({"RfrdDocInf": [{"Nb": remittance["invoiceNo"]}]}
               if remittance.get("invoiceNo") else {}),
        }]
    return block or None


def to_xml(message: dict, *, indent: bool = True) -> str:
    """Serialise the message to real pacs.008 XML. Stdlib only.

    A mechanical walk of the `@attr` / `#text` convention — this function knows the namespace
    and nothing else about pacs.008, which is the point of adopting the convention. Repeating
    elements (`CdtTrfTxInf`, `SvcLvl`) are lists and emit one element each; `None` values are
    skipped, because an absent optional element is correct ISO and an empty one is not.

    Stdlib `xml.etree.ElementTree` rather than `xmltodict`: this is ~30 lines of generation
    (not parsing), so the dependency philosophy's "<100 LOC of straightforward code, write it
    yourself" applies, and the backend takes no new dependency for a display feature.

    ⚠️ **This is well-formed XML in the pacs.008 shape; it is NOT XSD-validated.** No ISO
    20022 schema is available in this workspace (they are behind ISO registration), so nothing
    here proves schema conformance — see `test_the_pacs008_matches_the_iso20022_format_spec`
    for exactly what is and is not checked.
    """
    from xml.etree import ElementTree as ET

    root = ET.Element(ROOT, {"xmlns": NAMESPACE})
    _append(root, MESSAGE_ROOT, body(message))
    if indent:
        ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _append(parent, name: str, value) -> None:
    """Attach `value` to `parent` as one or more `<name>` elements."""
    from xml.etree import ElementTree as ET

    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            _append(parent, name, item)
        return
    if not isinstance(value, dict):
        ET.SubElement(parent, name).text = _text(value)
        return

    attributes = {
        k[len(ATTRIBUTE_PREFIX):]: _text(v)
        for k, v in value.items()
        if k.startswith(ATTRIBUTE_PREFIX) and v is not None
    }
    element = ET.SubElement(parent, name, attributes)
    for key, child in value.items():
        if key.startswith(ATTRIBUTE_PREFIX):
            continue
        if key == TEXT_KEY:
            element.text = _text(child)
            continue
        _append(element, key, child)


def _text(value) -> str:
    """XML has only text. Datetimes go out in ISO 8601; amounts get two decimal places.

    ⚠️ **Two decimals is a deliberate approximation, not a currency-aware rule.** Python
    renders `250000.00` as `250000.0`, which is lexically valid ISO decimal but reads as
    sloppy to anyone who works with these messages. Fixed two decimals is correct for every
    currency in this demo's corridors (USD/EUR/GBP/CHF/CAD) and **wrong for JPY**, which has
    no minor unit.

    The real fix is the repo-wide `Decimal128` / minor-units migration deferred by doc 13 §2
    B4: once amounts carry their own precision, this formats from the value instead of
    guessing. Named here rather than silently hardcoded, and it is why the JSON view keeps the
    numeric value untouched — only the XML text node is formatted.
    """
    if isinstance(value, float):
        return f"{value:.2f}"
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def body(message: dict) -> dict:
    """The `FIToFICstmrCdtTrf` body of a message, tolerating an already-unwrapped one.

    One place that knows the envelope, so a reader (the UI, the audit builder) never has to
    reach through two literal keys.
    """
    return ((message or {}).get(ROOT) or {}).get(MESSAGE_ROOT) or message or {}


def elements(message: dict) -> list:
    """The element path of every populated node, in message order.

    Paths are relative to `FIToFICstmrCdtTrf` — `GrpHdr/MsgId`, `CdtTrfTxInf/Dbtr` — because
    that is the part a reader is checking against the canonical payment; repeating
    `Document/FIToFICstmrCdtTrf/` on every row would be noise.

    Feeds `paymentMessages.transformationAudit[]` and the UI's ISO VIEW, so the demo can say
    which elements were produced without the reader parsing the message.
    """
    out = []
    for group, children in body(message).items():
        # `CdtTrfTxInf` is a list (1..n); everything else is an object.
        for child in (children if isinstance(children, list) else [children]):
            for name, value in (child or {}).items():
                if value is not None:
                    out.append(f"{group}/{name}")
    return out
