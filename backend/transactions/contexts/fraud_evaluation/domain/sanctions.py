"""Sanctions & AML screening — the decision, pure.

Doina's stage-4 demo display (L512) asks for *"Sanctions and AML screening passed"*. Before
this module, `payment_document.py` wrote `correspondent.sanctionsCheck` as a hardcoded
`{"status": "CLEAR", "provider": "PROV-SYNTH"}` **at initiation** — so every payment
asserted a screening that never ran, from the moment it was created (doc 18 B6). Initiation
now writes the spec's `PENDING`, which is true, and this module produces the real outcome in
stage 4b.

## ⚠️ SIMULATED, and every string it emits says so

The list below is a demo constant, not a sanctions list. Three reasons it is a module
constant rather than a seeded collection — a deliberate deviation from doc 18 B6's
"seed it and read it through the `ReferenceData` port":

1. It needs **no new collection on a shared database**. `leafy_bank_bian` already holds
   collections belonging to four other demos, and adding an unratified one is the very
   surface B2 warns about.
2. `customers.screening` and the `fraud*` collections already exist and are
   **ThreatSight360's** (12,766 documents, a different domain's shape). Screening against
   them is the ownership problem one step softer, so we screen against neither.
3. It keeps the whole rule pure and the tests hermetic. A screening decision is the last
   place to want a database round-trip inside the money path.

If a later phase wants a real list, the port is the right home and this constant is the
thing it replaces. Q30 is the conversation that has to happen first.

`status` values are the spec's own enum for `correspondent.sanctionsCheck.status`:
`CLEAR | HIT | PENDING | BLOCKED` — never hand-rolled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

CLEAR = "CLEAR"
HIT = "HIT"
PENDING = "PENDING"
BLOCKED = "BLOCKED"

PROVIDER = "SIMULATED-SCREENING-v1"

# Countries under comprehensive restriction in the demo narrative. Two-letter ISO codes.
_RESTRICTED_COUNTRIES = frozenset({"IR", "KP", "SY", "CU"})

# Denied parties, upper-cased for matching. Names invented for the demo — no real
# designated person or entity appears here, deliberately.
_DENIED_PARTIES = frozenset({
    "VOSTOK HEAVY INDUSTRIES",
    "PYONGYANG TRADING COLLECTIVE",
    "ACME SANCTIONED HOLDINGS",
})

# Potential matches (cutoff plan A2): names close enough to a list entry that an analyst
# must decide. Exact upper-case match, so the rule is deterministic. Invented for the demo;
# no simulator, autofill or recon scenario uses them (asserted by a test), so untagged
# traffic never meets one — and if it did, PENDING is a WARN that continues.
_POTENTIAL_MATCH_PARTIES = frozenset({
    "NORTHGATE TRADING FZE",
    "SEVERN MARITIME LLC",
})

# High-risk purpose codes. `purposeCodes` is a real reference collection (stage 3 resolves
# it), so this is a risk *classification* over real codes rather than invented data.
_HIGH_RISK_PURPOSE_CODES = frozenset({"CASH", "CGDD"})


@dataclass(frozen=True)
class ScreeningOutcome:
    """One screening result, shaped for `correspondent.sanctionsCheck` plus a check detail."""

    status: str
    detail: str
    matched: Optional[str] = None

    @property
    def refuses(self) -> bool:
        """A HIT or a BLOCK stops the payment. Nothing else does.

        Screening is the one control in this demo that must not proceed on doubt — unlike a
        thin bank directory, where a miss is a WARN (doc 17 B7). But note what is NOT a
        refusal: an **unscreenable** party. That records PENDING, never a false CLEAR.
        """
        return self.status in (HIT, BLOCKED)


def screen(
    *,
    creditor_name: Optional[str] = None,
    creditor_country: Optional[str] = None,
    purpose_code: Optional[str] = None,
    party_name: Optional[str] = None,
    party_country: Optional[str] = None,
    party_label: str = "Beneficiary",
) -> ScreeningOutcome:
    """Screen a party against the simulated lists. Total: always returns an outcome.

    Outbound screens the **beneficiary** — the party we are sending funds to — via the
    `creditor_*` arguments, which every existing caller uses and which stay as they are.

    Inbound screens the **originator** instead (FR-3.IN1). Her L703: incoming needs the
    mirror control performed earlier in the lifecycle, *"screening the party Leafy Bank is
    about to accept funds from, before any credit is posted — accepting funds is itself a
    compliance-relevant act, not just releasing them."* The `party_*` arguments express
    that, and `party_label` keeps the emitted prose honest about which side was screened.

    The RULES are identical for both directions and deliberately not duplicated: a denied
    party is denied whichever way the money flows. Only the subject changes.
    """
    name = party_name if party_name is not None else creditor_name
    country = party_country if party_country is not None else creditor_country
    if not name and not country:
        return ScreeningOutcome(
            PENDING,
            f"{party_label} carries neither a name nor a country, so no screening was "
            f"possible. Recorded PENDING rather than CLEAR ({PROVIDER}).",
        )
    creditor_name, creditor_country = name, country

    country = (creditor_country or "").upper()
    if country in _RESTRICTED_COUNTRIES:
        return ScreeningOutcome(
            BLOCKED,
            f"{party_label} country {country} is under comprehensive restriction "
            f"({PROVIDER}, SIMULATED).",
            matched=country,
        )

    name = (creditor_name or "").upper().strip()
    if name in _DENIED_PARTIES:
        return ScreeningOutcome(
            HIT,
            f"{party_label} '{creditor_name}' matches a denied-party entry "
            f"({PROVIDER}, SIMULATED).",
            matched=name,
        )

    if name in _POTENTIAL_MATCH_PARTIES:
        return ScreeningOutcome(
            PENDING,
            f"{party_label} '{creditor_name}' is a potential match against a list entry — "
            f"analyst review required ({PROVIDER}, SIMULATED).",
            matched=name,
        )

    if purpose_code and purpose_code.upper() in _HIGH_RISK_PURPOSE_CODES:
        # Not a hit. An AML-relevant purpose raises attention, and the fraud score is where
        # that attention is expressed (`fraud_rules.purpose_code_risk`) — screening still
        # reports CLEAR, because no list matched.
        return ScreeningOutcome(
            CLEAR,
            f"No sanctions match. Purpose code {purpose_code.upper()} is AML-relevant and "
            f"is scored by fraud evaluation ({PROVIDER}, SIMULATED).",
        )

    return ScreeningOutcome(
        CLEAR,
        f"No sanctions or denied-party match for '{creditor_name}' "
        f"({country or 'country unknown'}) ({PROVIDER}, SIMULATED).",
    )


def high_risk_purpose_codes() -> frozenset:
    return _HIGH_RISK_PURPOSE_CODES


def denied_parties() -> frozenset:
    return _DENIED_PARTIES


def restricted_countries() -> frozenset:
    return _RESTRICTED_COUNTRIES


def potential_matches() -> frozenset:
    return _POTENTIAL_MATCH_PARTIES
