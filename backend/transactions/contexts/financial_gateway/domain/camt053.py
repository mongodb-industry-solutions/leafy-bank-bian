"""Correspondent statement -> ISO 20022 camt.053.001.08 (BankToCustomerStatementV08). Pure.

The end-of-day statement our correspondent bank sends for Leafy Bank's nostro (GL 1111). It
is the reconciliation's **second system of record** — the external view that stage 8 compares
our books against (plan-reconciliation-agent.md §A1). Before this existed, `settle.py` wrote
`actualAmount` itself, so leg 2 compared us with ourselves.

## The lever: `simulatedStatementOutcome`

How the correspondent's line for one payment differs from our books. Captured at initiation,
persisted on the payment, applied here. `None` means CLEAN.

| value               | the line                                                          |
|---------------------|-------------------------------------------------------------------|
| `CLEAN`             | full amount, reference = paymentId                                |
| `FEE_DEDUCTED`      | amount − 25.00, `Chrgs/TtlChrgsAndTaxAmt` 25.00 (Doina's $25)     |
| `REFERENCE_ALTERED` | same amount, reference re-keyed by the correspondent              |
| `LATE`              | omitted from the first statement, booked on the next one          |
| `AMOUNT_TRANSPOSED` | two adjacent digits swapped (a keying error)                      |

LATE is decided by the application layer (it needs statement history); this module only
builds lines for what it is given.

## The reference is `paymentId`

Outbound payments carry no `endToEndId` on the canonical doc (it is set only for inbound,
from the sender's pacs.008), so the correspondent echoes our `paymentId`. That is the key
statement matching (A2) auto-matches on.

## Same provenance caveat as pacs002.py

No ISO schema is available in the workspace; the structure is from the published camt.053
message definition and is **not XSD-validated**. `test_statement_camt053.py` checks the
envelope, root, that `Ntry`/`Bal` are lists, and that none of our annotations leak into XML.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Optional

from contexts.payment_rail.domain import pacs008

MESSAGE_FORMAT = "camt.053.001.08"
MESSAGE_STANDARD = "ISO20022"
MAPPING_VERSION = "1.0.0"

ROOT = pacs008.ROOT
MESSAGE_ROOT = "BkToCstmrStmt"
NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"

CLEAN = "CLEAN"
FEE_DEDUCTED = "FEE_DEDUCTED"
REFERENCE_ALTERED = "REFERENCE_ALTERED"
LATE = "LATE"
AMOUNT_TRANSPOSED = "AMOUNT_TRANSPOSED"
STATEMENT_OUTCOMES = (CLEAN, FEE_DEDUCTED, REFERENCE_ALTERED, LATE, AMOUNT_TRANSPOSED)

# The intermediary's deducted charge — Doina's $25 story (Sep 17 L1264-1270). The single
# source since A2 retired settle.py's copy (reconciliation plan Decision 1).
CORRESPONDENT_CHARGE = 25.0

# The correspondent's own suffix when it re-keys a reference (REFERENCE_ALTERED).
_CORRESPONDENT_REF_SUFFIX = "/LEAFYBK"
_MAX_REF_LEN = 16

ORPHAN_REF_PREFIX = "ORPH-"

# Balance type codes (ISO ExternalBalanceType1Code).
_OPENING_BOOKED = "OPBD"
_CLOSING_BOOKED = "CLBD"
_DEBIT = "DBIT"
_CREDIT = "CRDT"

# Our annotation of a line — never part of the ISO payload. A2 writes it.
RECON_UNMATCHED = "UNMATCHED"


def altered_reference(payment_id: str) -> str:
    """`PAY-8ebaa746` -> `8EBAA746/LEAFYBK`: prefix dropped, upper-cased, suffixed, ≤16."""
    core = payment_id.split("-", 1)[-1].upper()
    return (core + _CORRESPONDENT_REF_SUFFIX)[:_MAX_REF_LEN]


def transposed_amount(amount: float) -> float:
    """Swap the lowest-order adjacent pair of differing whole-unit digits (1,250 -> 1,205).

    Scans the integer part right to left for two adjacent digits that differ. Returns the
    amount unchanged when there is none (1,111.00, 5.00): a transposition of equal digits is
    invisible, so the line degrades to CLEAN rather than inventing a different error.
    """
    whole, cents = f"{amount:.2f}".split(".")
    digits = list(whole)
    for i in range(len(digits) - 1, 0, -1):
        if digits[i] != digits[i - 1]:
            digits[i], digits[i - 1] = digits[i - 1], digits[i]
            return float(f"{''.join(digits)}.{cents}")
    return amount


def entry_for(position: dict, outcome: Optional[str]) -> dict:
    """The statement line the correspondent books for one of our settlement positions. Pure.

    Returns our flat projection (`entries[]` on the stored doc); `to_ntry` renders it as ISO.
    LATE is not handled here — the caller decides which statement a LATE line lands on.
    """
    outcome = outcome or CLEAN
    payment_id = position["paymentId"]
    amount = float(position.get("grossAmount") or position.get("expectedAmount") or 0.0)
    reference = payment_id
    charges = None

    if outcome == FEE_DEDUCTED:
        charges = min(CORRESPONDENT_CHARGE, amount)
        amount = round(amount - charges, 2)
    elif outcome == REFERENCE_ALTERED:
        reference = altered_reference(payment_id)
    elif outcome == AMOUNT_TRANSPOSED:
        amount = transposed_amount(amount)

    return {
        "reference": reference,
        "amount": amount,
        "currency": position.get("currency") or position.get("expectedCurrency") or "USD",
        "creditDebit": _DEBIT,          # an outbound wire leaves the nostro
        "charges": charges,
        # Simulator truth, kept for tests and the demo UI. Matching (A2) must never read these:
        # a real statement does not tell you which payment a line belongs to.
        "simulatedPaymentId": payment_id,
        "simulatedOutcome": outcome,
        "recon": _unmatched(),
    }


def orphan_entry(rng: random.Random, correspondent_bics: list[str]) -> dict:
    """One plausible line with no internal counterpart (the spec's "orphaned settlement")."""
    amount = float(rng.choice([1_250, 3_400, 7_800, 12_500, 18_750]))
    return {
        "reference": f"{ORPHAN_REF_PREFIX}{rng.randrange(16**8):08X}",
        "amount": amount,
        "currency": "USD",
        "creditDebit": _DEBIT,
        "charges": None,
        "counterpartyBic": rng.choice(correspondent_bics),
        "simulatedPaymentId": None,
        "simulatedOutcome": "ORPHAN",
        "recon": _unmatched(),
    }


def _unmatched() -> dict:
    return {"status": RECON_UNMATCHED, "matchedPaymentId": None, "matchedBy": None, "at": None}


def _amt(amount: float, currency: str) -> dict:
    return {f"{pacs008.ATTRIBUTE_PREFIX}Ccy": currency, pacs008.TEXT_KEY: amount}


def to_ntry(entry: dict, *, booking_date: datetime) -> dict:
    """Render one projected line as an ISO `Ntry`. Only ISO elements — no `recon`, no
    `simulated*`, which exist for us and would be a lie in a correspondent's message."""
    tx_details = {
        "Refs": {"EndToEndId": entry["reference"], "AcctSvcrRef": entry["reference"]},
        "Amt": _amt(entry["amount"], entry["currency"]),
    }
    if entry.get("charges"):
        tx_details["Chrgs"] = {"TtlChrgsAndTaxAmt": _amt(entry["charges"], entry["currency"])}
    if entry.get("counterpartyBic"):
        tx_details["RltdAgts"] = {"CdtrAgt": {"FinInstnId": {"BICFI": entry["counterpartyBic"]}}}
    return {
        "Amt": _amt(entry["amount"], entry["currency"]),
        "CdtDbtInd": entry["creditDebit"],
        "Sts": {"Cd": "BOOK"},
        "BookgDt": {"DtTm": booking_date},
        "ValDt": {"Dt": booking_date.date().isoformat()},
        "AcctSvcrRef": entry["reference"],
        "NtryDtls": [{"TxDtls": [tx_details]}],
    }


def closing_balance(opening: float, entries: list[dict]) -> float:
    signed = sum(-e["amount"] if e["creditDebit"] == _DEBIT else e["amount"] for e in entries)
    return round(opening + signed, 2)


def build(
    *,
    statement_id: str,
    account_code: str,
    currency: str,
    window_from: datetime,
    window_to: datetime,
    sequence: int,
    opening_balance: float,
    entries: list[dict],
    now: datetime,
) -> dict:
    """The camt.053 message for one statement. Pure — every value is an argument."""
    closing = closing_balance(opening_balance, entries)
    debits = [e for e in entries if e["creditDebit"] == _DEBIT]
    statement = {
        "Id": statement_id,
        "ElctrncSeqNb": sequence,
        "CreDtTm": now,
        "FrToDt": {"FrDtTm": window_from, "ToDtTm": window_to},
        "Acct": {"Id": {"Othr": {"Id": account_code}}, "Ccy": currency},
        "Bal": [
            _balance(_OPENING_BOOKED, opening_balance, currency, window_from),
            _balance(_CLOSING_BOOKED, closing, currency, window_to),
        ],
        "TxsSummry": {
            "TtlNtries": {"NbOfNtries": str(len(entries))},
            "TtlDbtNtries": {
                "NbOfNtries": str(len(debits)),
                "Sum": round(sum(e["amount"] for e in debits), 2),
            },
        },
        "Ntry": [to_ntry(e, booking_date=window_to) for e in entries],
    }
    return {ROOT: {MESSAGE_ROOT: {
        "GrpHdr": {"MsgId": statement_id, "CreDtTm": now},
        "Stmt": [statement],
    }}}


def _balance(code: str, amount: float, currency: str, at: datetime) -> dict:
    return {
        "Tp": {"CdOrPrtry": {"Cd": code}},
        "Amt": _amt(abs(amount), currency),
        "CdtDbtInd": _CREDIT if amount >= 0 else _DEBIT,
        "Dt": {"DtTm": at},
    }


def body(message: dict) -> dict:
    """The `BkToCstmrStmt` body, tolerating an already-unwrapped message."""
    return ((message or {}).get(ROOT) or {}).get(MESSAGE_ROOT) or message or {}


def to_xml(message: dict, *, indent: bool = True) -> str:
    """Serialise to camt.053 XML, reusing the pacs.008 mapper's `@attr`/`#text` walk."""
    from xml.etree import ElementTree as ET

    root = ET.Element(ROOT, {"xmlns": NAMESPACE})
    pacs008._append(root, MESSAGE_ROOT, body(message))
    if indent:
        ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
