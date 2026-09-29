"""The inbound message simulator — a plausible pacs.008 for a live beneficiary account. Pure.

The demo has no sending bank, so something has to stand in one. This module builds the
message the simulator sends: an external sender's pacs.008 naming a REAL Leafy Bank account
as the claimed beneficiary, shaped so each named scenario exercises a different inbound path.

## Built by the real mapper, never hand-written

The message is assembled by `pacs008.build` and then has exactly TWO elements rewritten —
`GrpHdr/InstgAgt` and `CdtTrfTxInf/DbtrAgt` — because the outbound mapper always stamps OUR
bank there (outbound, we genuinely are the debtor's agent), while an inbound message carries
the SENDING bank. Everything else comes from the production mapper, so the simulator can
never drift from the shape `inbound_pacs008.parse` reads. A hand-written literal here would
be defect 2026-09-02 (`fixture-fidelity`) in production code.

## Scenario coherence (defect 2026-09-09, `autofill-incoherence`)

Each sender row bundles name, BIC, bank name and country — one coherent originator, not
independent draws. The scenario then mutates ONE thing on purpose, and the mutation is named:

| scenario    | mutated                  | exercises                                    |
|-------------|--------------------------|----------------------------------------------|
| `HAPPY`     | nothing                  | the full path to SETTLED                      |
| `PARTIAL`   | the beneficiary name     | a plausible variant -> stage 3 with a flag   |
| `MISMATCH`  | the beneficiary name     | NO_MATCH -> the UTA queue                     |
| `SANCTIONS`| the ORIGINATOR name      | a screening hit despite a matched beneficiary |
| `FX`       | the instructed currency  | the inbound FX conversion (FR-3.IN2)          |

`DUPLICATE` mutates nothing and is not built here at all: a duplicate is the SAME message
sent twice, which the caller does by re-running one build's output — not a second build,
which would mint a fresh UETR and be no duplicate at all.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Optional
import uuid

from contexts.payment_order_initiation.domain import bank_identity
from contexts.payment_rail.domain import pacs008

SCENARIO_HAPPY = "HAPPY"
SCENARIO_PARTIAL = "PARTIAL"
SCENARIO_MISMATCH = "MISMATCH"
SCENARIO_SANCTIONS = "SANCTIONS"
SCENARIO_FX = "FX"
SCENARIO_DUPLICATE = "DUPLICATE"

SCENARIOS = (
    SCENARIO_HAPPY, SCENARIO_PARTIAL, SCENARIO_MISMATCH,
    SCENARIO_SANCTIONS, SCENARIO_FX, SCENARIO_DUPLICATE,
)

# One coherent originator per row (defect 2026-09-09): name, BIC, bank, country and account
# travel together. BIC characters 5-6 are the country (ISO 9362), which is what
# `_country_of` reads — so the country must agree with the BIC or the corridor
# classification and the sanctions screen would disagree about the same sender.
SENDERS = (
    {"name": "Acme Industrie GmbH", "bic": "DEUTDEFF", "bankName": "Deutsche Bank",
     "country": "DE", "accountNo": "DE89370400440532013000"},
    {"name": "Londinium Trading Ltd", "bic": "BARCGB22", "bankName": "Barclays",
     "country": "GB", "accountNo": "GB29NWBK60161331926819"},
    {"name": "Helvetia Components SA", "bic": "UBSWCHZH", "bankName": "UBS",
     "country": "CH", "accountNo": "CH9300762011623852957"},
    {"name": "Boreal Forestry OY", "bic": "NDEAFIHH", "bankName": "Nordea",
     "country": "FI", "accountNo": "FI2112345600000785"},
    {"name": "Atlas Freight LLC", "bic": "CHASUS33", "bankName": "JPMorgan Chase",
     "country": "US", "accountNo": "US-ACH-021000021-991177"},
)

# The reference currency for the FX scenario: a currency a Leafy Bank USD account is not
# denominated in, so the conversion genuinely fires. Rates come from
# `screen_and_accept._FX_RATES`, not from here — this only picks the pair.
_FX_SOURCE_CURRENCY = "EUR"


def partial_name(name: str) -> str:
    """A plausible variant of a real name — the typo a sending bank actually makes.

    One character of the first token changes. Deliberately deterministic (not random): the
    PARTIAL classification must be reproducible, and a random mutation could land on
    NO_MATCH by luck and turn the "proceeds with a flag" demo into the UTA one.
    """
    tokens = name.split()
    if not tokens:
        return name
    first = list(tokens[0])
    # Mutate the last character: "Frida" -> "Fride", "Holder" -> "Holdur"-style slippage.
    first[-1] = "e" if first[-1] != "e" else "a"
    tokens[0] = "".join(first)
    return " ".join(tokens)


def mismatch_name(name: str) -> str:
    """A name that shares nothing with the account holder — an unrelated claim."""
    return "Marcel Devereux"


def build_message(
    *,
    scenario: str,
    beneficiary_name: str,
    beneficiary_identifier: str,
    identifier_is_iban: bool,
    account_currency: str = "USD",
    amount: Optional[float] = None,
    sender: Optional[dict] = None,
    uetr: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """One inbound pacs.008 for the given scenario. Pure — no I/O.

    `beneficiary_identifier` is the IBAN when the account has one (the primary lookup —
    two accounts can share a display number), else the account number;
    `identifier_is_iban` says which, so the canonical creditor block carries it in the
    right field and the account-number fallback is not exercised by accident.
    """
    now = now or datetime.now(timezone.utc)
    sender = sender or random.choice(SENDERS)
    scenario = scenario.upper()

    # The instructed currency: FX deliberately differs from the beneficiary account's, every
    # other scenario pays in the account's own so the happy path is unconverted.
    currency = (
        _FX_SOURCE_CURRENCY if scenario == SCENARIO_FX and account_currency == "USD"
        else account_currency
    )
    amount = amount if amount is not None else round(random.uniform(1_000.0, 25_000.0), 2)

    # The claimed name, by scenario. HAPPY names the account holder exactly; PARTIAL
    # introduces the typo; MISMATCH claims someone else entirely. SANCTIONS leaves the
    # beneficiary matched and puts the problem on the ORIGINATOR — her L831's exact case.
    if scenario == SCENARIO_PARTIAL:
        claimed_name = partial_name(beneficiary_name)
    elif scenario == SCENARIO_MISMATCH:
        claimed_name = mismatch_name(beneficiary_name)
    else:
        claimed_name = beneficiary_name
    if scenario == SCENARIO_SANCTIONS:
        # From the screening module's denied-party list, not restated here — the simulator
        # must be able to hit the real rule, and a restated list could drift from it.
        from contexts.fraud_evaluation.domain import sanctions
        originator_name = sorted(sanctions.denied_parties())[0]
    else:
        originator_name = sender["name"]

    payment = {
        "msgId": f"MSG-{uuid.uuid4().hex[:12].upper()}",
        "initiatedAt": now,
        "instructionId": f"INSTR-{uuid.uuid4().hex[:10].upper()}",
        "endToEndId": f"E2E-{uuid.uuid4().hex[:12].upper()}",
        "txnId": f"TXN-{uuid.uuid4().hex[:10].upper()}",
        "uetr": uetr or f"UETR-{uuid.uuid4()}",
        "amount": amount,
        "currency": currency,
        "chargeBearer": "SHAR",
        "requestedExecutionDate": now.date().isoformat(),
        "debtor": {
            "name": originator_name,
            "accountNo": sender["accountNo"],
            "bic": sender["bic"],
            "bankName": sender["bankName"],
            "address": f"{sender['country']}",
        },
        "creditor": {
            "name": claimed_name,
            "accountNo": None if identifier_is_iban else beneficiary_identifier,
            "iban": beneficiary_identifier if identifier_is_iban else None,
            # The bank identity comes from `bank_identity` — the single definition — so the
            # simulator cannot disagree with it (guarded by
            # `test_our_bank_identity_has_exactly_one_definition`).
            "bic": bank_identity.OUR_BIC,
            "bankName": bank_identity.OUR_BANK_NAME,
            "address": f"{bank_identity.OUR_BANK_COUNTRY}",
        },
        "remittance": {
            "unstructured": f"Invoice {random.randint(1000, 9999)}",
            "reference": f"REF-{random.randint(10000, 99999)}",
            # SUPP is a real purposeCodes value — an invented code would be "corrected"
            # by enrichment and show a spurious diff (the autofill-corollary defect).
            "purposeCode": "SUPP",
        },
        "correspondent": {},
        "clearing": {},
        "wireDetails": {},
    }
    message = pacs008.build(payment)
    return _sender_is_the_sending_bank(message, sender)


def _sender_is_the_sending_bank(message: dict, sender: dict) -> dict:
    """Make `InstgAgt`/`DbtrAgt` the sender's bank, as a real inbound message has them.

    The outbound mapper stamps OUR identity on both (correctly, for a payment we initiate).
    An inbound message instructs FROM the sending bank, so both are rewritten to it — two
    elements, and nothing else, so the rest of the shape stays the mapper's.
    """
    agent = {"FinInstnId": {"BICFI": sender["bic"], "Nm": sender["bankName"]}}
    body = pacs008.body(message)
    body["GrpHdr"]["InstgAgt"] = agent
    body["CdtTrfTxInf"][0]["DbtrAgt"] = agent
    return message
