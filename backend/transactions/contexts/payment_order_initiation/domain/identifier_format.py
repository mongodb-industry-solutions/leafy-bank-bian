"""Structural validation of payment identifiers — BIC, IBAN, ABA. Doc 17 §3 step 3 (R8).

Doina L437: *"Beneficiary validation ✓ Beneficiary recognized ✓ **Account format valid**"*.
Nothing in the service validated any identifier's shape before this: a creditor BIC was
accepted as typed and an ABA routing number was passed through unexamined, so a transposed
digit reached the payment document and would have reached a rail message.

Pure. No I/O, no clock, no reference-data lookup. **Format only** — whether an identifier is
*well-formed*, never whether the institution exists. Existence is the directory's question
(R7/R13, `ReferenceData`), and the two failures are deliberately different outcomes: a
malformed identifier is a refusal (the caller made an error we can name), an unknown-but-
well-formed one is a WARN (our directory may simply be thin). Doc 17 B7.

Each validator returns `None` when the value is acceptable and a human-readable reason when
it is not, so a caller can put the reason straight into a `checks[]` `detail` without
restating the rule. `None` input is acceptable everywhere — an unenriched payment legitimately
has `creditor.bic: None`, and requiredness is the request contract's job, not this module's.
"""

from __future__ import annotations

import re
from typing import Optional

# ISO 9362: 4-letter institution code, 2-letter ISO-3166 country, 2-char location, and an
# optional 3-char branch. 8 or 11 characters, never 9 or 10 — the length rule catches the
# most common paste error on its own.
_BIC = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")

# ISO 13616: 2-letter country, 2 check digits, up to 30 alphanumeric characters.
_IBAN = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$")

_ABA_LENGTH = 9

# The Federal Reserve's own weighting for the ABA check digit.
_ABA_WEIGHTS = (3, 7, 1, 3, 7, 1, 3, 7, 1)


def bic_problem(bic: Optional[str]) -> Optional[str]:
    """Why `bic` is not a well-formed ISO 9362 BIC, or None."""
    if bic is None or bic == "":
        return None
    if not isinstance(bic, str):
        return f"BIC {bic!r} is not a string."
    if len(bic) not in (8, 11):
        return f"BIC {bic!r} is {len(bic)} characters; ISO 9362 allows 8 or 11."
    if not _BIC.match(bic):
        return (
            f"BIC {bic!r} is not ISO 9362 shaped "
            "(4 letters, 2-letter country, 2 alphanumeric, optional 3-character branch)."
        )
    return None


def iban_problem(iban: Optional[str]) -> Optional[str]:
    """Why `iban` is not a well-formed IBAN, or None.

    Checks the ISO 13616 shape and then the mod-97 check digits, which is the part that
    catches a transposition — a shape-only check passes almost any typo.
    """
    if iban is None or iban == "":
        return None
    if not isinstance(iban, str):
        return f"IBAN {iban!r} is not a string."
    compact = iban.replace(" ", "").upper()
    if not _IBAN.match(compact):
        return (
            f"IBAN {iban!r} is not ISO 13616 shaped "
            "(2-letter country, 2 check digits, then up to 30 alphanumeric characters)."
        )
    # Move the first four characters to the end, map letters to 10-35, take mod 97.
    rearranged = compact[4:] + compact[:4]
    digits = "".join(
        str(int(c, 36)) if c.isalpha() else c for c in rearranged
    )
    if int(digits) % 97 != 1:
        return f"IBAN {iban!r} fails the mod-97 check digits."
    return None


def aba_problem(aba: Optional[str]) -> Optional[str]:
    """Why `aba` is not a well-formed ABA routing number, or None.

    Nine digits with the Fed's weighted mod-10 checksum. Applies to `clearingSystemCode`
    `USABA` only — other clearing systems have their own rules, and this module claims none
    of them (see `clearing_member_problem`).
    """
    if aba is None or aba == "":
        return None
    if not isinstance(aba, str):
        return f"Routing number {aba!r} is not a string."
    if len(aba) != _ABA_LENGTH or not aba.isdigit():
        return f"Routing number {aba!r} is not {_ABA_LENGTH} digits."
    total = sum(w * int(d) for w, d in zip(_ABA_WEIGHTS, aba))
    if total % 10 != 0:
        return f"Routing number {aba!r} fails the ABA mod-10 checksum."
    return None


def clearing_member_problem(
    clearing_system_code: Optional[str], member_id: Optional[str]
) -> Optional[str]:
    """Why `member_id` is not well-formed for its clearing system, or None.

    Only `USABA` has a checksum rule implemented. For every other system the member id is
    accepted on presence alone — the sort-code / BLZ / transit-number rules are real but
    unimplemented, and inventing a rule would refuse valid payments. That is a deliberate
    gap, recorded here rather than hidden: it is why the check's `detail` says *not
    validated* for a non-US corridor instead of claiming a pass.
    """
    if not member_id:
        return None
    if clearing_system_code == "USABA":
        return aba_problem(member_id)
    return None


def clearing_member_is_validated(clearing_system_code: Optional[str]) -> bool:
    """Whether `clearing_member_problem` actually checked anything for this system.

    Lets a caller record an honest `detail` — "validated" vs "format not validated for
    <system>" — without duplicating the knowledge of which systems have a rule.
    """
    return clearing_system_code == "USABA"
