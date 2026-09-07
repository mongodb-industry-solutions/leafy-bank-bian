"""Stage 2 entitlement policy — pure. No I/O, no Mongo, no clock, no env at import.

BIAN CustomerAccessEntitlement (SD 43057, control record `CustomerAccessProfileAgreement`).

## Why a policy table lives in the service and not in the data model

**There is no limit field anywhere in the canonical model** — not on `accounts`, not on
`customers`, not on `payments`. `accounts.signatories[]` carries `signingRule`
(`SOLE|JOINT|ANY_TWO`) and nothing monetary; doc 12's claim that signatory limits live
there is false (doc 15 B2, and the appendix that retired the claim).

So entitlement limits are keyed on `customers.segment`, a field that does exist, and the
table lives here until Doina rules on Q14 ("should limits be modelled on `accounts`?").
Shaped deliberately so that answer migrates without rework: the callers below ask
questions ("is this amount allowed?"), never read the table.

The stage-1 global `PAYMENT_LIMIT_USD` guard (`capture.py`) is now only a sanity bound on
malformed input. The real entitlement decision is here.

Env overrides exist so a demo can be retuned without a deploy of new code. They are read
**lazily inside `policy_for`**, never at module scope — a module-level `os.getenv` runs
before `load_dotenv` and silently takes the default (defects.md 2026-06-30).
"""

from __future__ import annotations

import os
from typing import Optional

# `customers.segment` enum, verbatim from the canonical spec (`CustomerSegmentType`).
# Never hand-roll an enum here — drift between code and spec has bitten this project
# (umbrella defects.md 2026-04-28, `enum-drift`).
SEGMENTS = ("RETAIL", "COMMERCIAL", "PRIVATE", "SME")

# `accounts.signatories[].signingRule` enum (`AccountSignatorySigningRuleType`).
SIGNING_RULES = ("SOLE", "JOINT", "ANY_TWO")

# Methods strong enough to stand alone above a step-up threshold. PASSWORD and API_KEY
# are single-factor knowledge/possession secrets, so they need a second factor.
_STRONG_METHODS = frozenset({"OTP", "BIOMETRIC", "MTLS"})

# `perPaymentLimit`       — the entitlement ceiling for one payment (R7).
# `dualApprovalThreshold` — above this a second approver is required (R8/R9).
#                           COMMERCIAL is 10_000 to match Doina's scenario exactly:
#                           $25,000 from a corporate → "second corporate approver required".
# `stepUpThreshold`       — above this a single weak factor is not enough (R1 step-up).
_DEFAULT_POLICY = {
    "RETAIL":     {"perPaymentLimit":  25_000.0, "dualApprovalThreshold": 10_000.0, "stepUpThreshold":  2_500.0},
    "COMMERCIAL": {"perPaymentLimit": 500_000.0, "dualApprovalThreshold": 10_000.0, "stepUpThreshold": 10_000.0},
    "PRIVATE":    {"perPaymentLimit": 250_000.0, "dualApprovalThreshold": 50_000.0, "stepUpThreshold": 25_000.0},
    "SME":        {"perPaymentLimit": 100_000.0, "dualApprovalThreshold": 25_000.0, "stepUpThreshold": 10_000.0},
}

# An unknown or missing segment gets the tightest policy on the table rather than a
# permissive default — a payment whose entitlement we cannot establish is not entitled.
_FALLBACK_SEGMENT = "RETAIL"

_ENV_KEY = {
    "perPaymentLimit": "ENTITLEMENT_PER_PAYMENT_LIMIT",
    "dualApprovalThreshold": "ENTITLEMENT_DUAL_APPROVAL_THRESHOLD",
    "stepUpThreshold": "ENTITLEMENT_STEP_UP_THRESHOLD",
}


def policy_for(segment: Optional[str]) -> dict:
    """The effective policy for a `customers.segment` value.

    Env override per key per segment, e.g. `ENTITLEMENT_DUAL_APPROVAL_THRESHOLD_COMMERCIAL`.
    A malformed override is ignored rather than fatal: a typo in a demo's deploy config
    must not take the payment path down, and the default it falls back to is the stricter
    published number.
    """
    key = segment if segment in _DEFAULT_POLICY else _FALLBACK_SEGMENT
    defaults = _DEFAULT_POLICY[key]
    return {name: _override(f"{_ENV_KEY[name]}_{key}", value) for name, value in defaults.items()}


def _override(env_name: str, default: float) -> float:
    raw = os.getenv(env_name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def signatory_for(signatories, customer_id: Optional[str]) -> Optional[dict]:
    """The caller's own entry in `accounts.signatories[]`, or None if absent.

    None is the entitlement answer, not an error: a party who is not a signatory on the
    account has no authority to debit it, however the account came to name them elsewhere.
    """
    for entry in signatories or []:
        if isinstance(entry, dict) and entry.get("customerId") == customer_id:
            return entry
    return None


def approval_required(amount: float, segment: Optional[str]) -> bool:
    """Does this payment need a second approver? (R8/R9)

    Amount-threshold only — Doina's "$25k -> payment > $10k -> second corporate approver
    required". A multi-signer mandate does not itself trigger dual approval at any amount;
    the amount is the trigger. (Corrected the earlier "always, whatever the amount" rule,
    which made every sub-threshold payment on a JOINT account read as dual-approved and
    produced a message claiming an amount was above a threshold it was not.)
    """
    return amount > policy_for(segment)["dualApprovalThreshold"]


def limit_available(amount: float, segment: Optional[str]) -> bool:
    """Is `amount` within the segment's per-payment entitlement? (R7)"""
    return amount <= policy_for(segment)["perPaymentLimit"]


def authentication_sufficient(
    method: Optional[str],
    factor_count: int,
    amount: float,
    segment: Optional[str],
) -> bool:
    """Is the channel's asserted authentication strong enough for this amount? (R1)

    `NONE` and a missing method are never sufficient — an absent authentication is not a
    weak one, it is no authentication (B1). Above the step-up threshold a single weak
    factor no longer carries the amount.
    """
    if not method or method == "NONE":
        return False
    if amount <= policy_for(segment)["stepUpThreshold"]:
        return True
    return factor_count >= 2 or method in _STRONG_METHODS


def blocking_restrictions(restrictions, *, now, side: str = "DEBIT") -> list:
    """The active restrictions on an account that forbid movement on `side` (R6).

    BIAN calls this `CustomerAccessEntitlement/Restrictions/Evaluate`; the data is
    `accounts.restrictions[]` (`AccountRestrictionType`), which no code read before this
    stage — a FROZEN or legally-held account could be debited.

    `expiresAt` is honoured: a lapsed restriction does not block. It is an ISO date string
    in the seed data and a datetime once a service writes one, so both are compared
    against `now` as strings — ISO-8601 sorts lexicographically, which is the whole reason
    the spec stores dates that way.
    """
    blocking = {"FULL_BLOCK", "LEGAL_HOLD", f"{side}_BLOCK"}
    return [
        r for r in (restrictions or [])
        if isinstance(r, dict) and r.get("type") in blocking and not _expired(r.get("expiresAt"), now)
    ]


def _expired(expires_at, now) -> bool:
    if not expires_at:
        return False
    return _as_iso(expires_at) <= _as_iso(now)


def _as_iso(value) -> str:
    return value if isinstance(value, str) else value.isoformat()
