"""Beneficiary name matching for inbound payments (FR-2.IN3). Pure — no I/O.

Her L496 gives the scenario: an inbound wire names account `****1234`, beneficiary "Frida
Nilsen"; we compare that against the account holder of record and classify the result three
ways, because the three outcomes take different downstream paths (her L498-502):

| outcome     | meaning                                        | path                    |
|-------------|------------------------------------------------|-------------------------|
| `MATCHED`   | account exists, open, name matches             | stage 3                 |
| `PARTIAL`   | account exists/open, name a plausible variant  | stage 3, with a flag    |
| `NO_MATCH`  | closed, missing, or name unrelated             | stage 9 (Unable to Apply)|

## Why three outcomes and not a score

A confidence number would need a threshold, and a threshold here decides whether a customer
gets their money — so the classification is explicit and inspectable instead. Her demo panel
prints the outcome by name ("Closest match: Frida Nilsen ... (confidence: PARTIAL)"), which
only works if the outcome IS the vocabulary rather than a bucketed float.

## Deliberately simple, and honest about it

Real name screening is a specialist product (transliteration, cultural name order, corporate
suffixes, fuzzy phonetics). This is a demo: normalise, then compare exactly, then compare on
a token basis. **No fuzzy library, no edit distance.** A "Fred/Frida" typo lands in
`PARTIAL` via the surname-token rule, which is the case her demo panel actually shows, and
anything less similar is `NO_MATCH` — which routes to a human, the correct failure mode.
"""

from __future__ import annotations

import re
from typing import Optional

MATCHED = "MATCHED"
PARTIAL = "PARTIAL"
NO_MATCH = "NO_MATCH"

# The outcomes that let a payment proceed to stage 3. `PARTIAL` proceeds *with a flag* —
# her L500 — because a plausible variant is not grounds to refuse a customer's money, but it
# is grounds to say so on the record.
PROCEEDING_OUTCOMES = frozenset({MATCHED, PARTIAL})

METHOD_EXACT = "EXACT"
METHOD_NORMALISED = "NORMALISED"
METHOD_TOKEN = "TOKEN"
METHOD_NONE = "NONE"
# Set by an operator's Repair (FR-9.IN2), never by this module — a human confirming the
# beneficiary is a different kind of evidence from an algorithm matching a string, and the
# stored `matchMethod` must be able to say which one happened.
METHOD_MANUAL_REPAIR = "MANUAL_REPAIR"

# Corporate suffixes and honorifics carry no identifying information and differ freely
# between a bank's records and a sender's message ("Acme Ltd" vs "Acme Limited").
_NOISE_TOKENS = frozenset({
    "ltd", "limited", "llc", "inc", "incorporated", "plc", "gmbh", "ag", "sa", "nv", "bv",
    "co", "company", "corp", "corporation", "sarl", "spa", "pty", "oy", "ab", "as",
    "mr", "mrs", "ms", "miss", "dr", "prof",
})


def normalise(name: Optional[str]) -> str:
    """Case, punctuation and whitespace carry no identity. Strip them all."""
    if not name:
        return ""
    lowered = re.sub(r"[^\w\s]", " ", name.lower())
    return " ".join(lowered.split())


def _tokens(name: str) -> list:
    return [t for t in normalise(name).split() if t not in _NOISE_TOKENS]


def compare(claimed: Optional[str], of_record: Optional[str]) -> tuple:
    """`(outcome, method)` for a claimed beneficiary name against the account holder.

    Returns `NO_MATCH` when either side is missing: an unnamed beneficiary cannot be
    confirmed, and confirming it anyway would be the worst of the three answers.
    """
    if not claimed or not of_record:
        return NO_MATCH, METHOD_NONE

    if claimed.strip() == of_record.strip():
        return MATCHED, METHOD_EXACT

    claimed_normal, record_normal = normalise(claimed), normalise(of_record)
    if claimed_normal == record_normal:
        # Differs only by case/punctuation/spacing — the same name.
        return MATCHED, METHOD_NORMALISED

    claimed_tokens, record_tokens = set(_tokens(claimed)), set(_tokens(record_normal))
    if not claimed_tokens or not record_tokens:
        return NO_MATCH, METHOD_NONE

    if claimed_tokens == record_tokens:
        # Same tokens, different order ("Nilsen Frida") or a dropped suffix.
        return MATCHED, METHOD_TOKEN

    shared = claimed_tokens & record_tokens
    if shared:
        # Some but not all name parts agree: a middle name present on one side, a maiden
        # name, or a first-name typo where the surname still matches. Her flagship PARTIAL
        # case ("Fred Nilsen" vs "Frida Nilsen") lands exactly here. Plausible, unproven —
        # which is what PARTIAL means.
        return PARTIAL, METHOD_TOKEN

    return NO_MATCH, METHOD_TOKEN
