"""Stage 4b — fraud scoring rules. Pure.

BIAN FraudEvaluation (SD 44625), which **does** have a published semantic API. Its own
behavioural qualifiers are `RuleSetsandDecisionTrees` and `Models` — so Doina's L511
*"Risk assessment completed (rules + model score)"* is the SD's structure, not a phrase to
paraphrase. This module is the `RuleSetsandDecisionTrees` half.

## The model half, honestly

There is no model. `MODEL_ID`/`MODEL_VERSION` below name a **constant baseline**, labelled
as one everywhere it surfaces, and Q29 asks Doina whether she wants it labelled, dropped
from the display line, or pointed at a real model. Inventing a score and calling it a model
would be the worst of the three (doc 18 B8's rejected alternatives).

## Every rule reads data that exists

Doc 18 B8 is explicit about this: a rule over a field no caller can populate is a rule that
can never fire, and a control that can only pass is half-built — the 2026-08-31 defect in
its purest form. So `device/channel` signals are **absent** here, not stubbed:
`initiation.ipAddress`/`deviceId` are written `None` and `extra="forbid"` means a caller
cannot send them. They come back when the contract admits them.

`velocity` and `new_beneficiary` need counts from the database. They are **parameters**, not
lookups — the caller queries and passes the numbers in, which is what keeps this module
pure and its tests hermetic. Same split as stage 3's `duplicate_detection`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Decisions — the spec's own `fraud.decision` enum, never hand-rolled (defect 2026-04-28).
APPROVED = "APPROVED"
REVIEW = "REVIEW"
DECLINED = "DECLINED"

DECISIONS = (APPROVED, REVIEW, DECLINED)

# `fraud.score` is `0-100` in the spec, so the sum is clamped rather than allowed to run off
# the top: a score of 140 would be invalid against the schema even though it is arithmetic.
SCORE_MIN = 0
SCORE_MAX = 100

# Thresholds. Constants here, not env vars: they are demo narrative, and the flagship
# $25,000 corporate wire must land APPROVED (D9). Change them here, and update
# `_state.md`'s "numbers baked into the demo" list in the same edit.
REVIEW_THRESHOLD = 50
DECLINE_THRESHOLD = 80

# The baseline that stands in for a model score. See the module docstring — Q29.
MODEL_ID = "SIMULATED-BASELINE"
MODEL_VERSION = "v1"
MODEL_BASELINE_SCORE = 4

# Amount bands, in the payment's own currency. The demo is USD throughout (stage 3 refuses
# any currency mismatch), so no FX normalisation is attempted — doing it with `fxRate`,
# which has no write path anywhere, would be fabricated precision.
_AMOUNT_BANDS = [
    (1_000_000, 45, "at or above 1,000,000"),
    (250_000, 30, "at or above 250,000"),
    (100_000, 18, "at or above 100,000"),
    (25_000, 10, "at or above 25,000"),
    (10_000, 5, "at or above 10,000"),
]

_CROSS_BORDER_WEIGHT = 12
_NEW_BENEFICIARY_WEIGHT = 15
_HIGH_RISK_PURPOSE_WEIGHT = 20

# Velocity: how many payments from this debtor inside the window before it is notable.
VELOCITY_THRESHOLD = 3
_VELOCITY_WEIGHT = 18


@dataclass(frozen=True)
class RuleOutcome:
    name: str
    fired: bool
    weight: int
    detail: str


@dataclass(frozen=True)
class FraudAssessment:
    score: int
    decision: str
    rules_fired: list = field(default_factory=list)
    outcomes: list = field(default_factory=list)
    model_id: str = MODEL_ID
    model_version: str = MODEL_VERSION

    @property
    def refuses(self) -> bool:
        return self.decision == DECLINED

    @property
    def holds(self) -> bool:
        """REVIEW: neither approved nor refused. The payment stops without being rejected.

        There is no lifecycle state for this — `PENDING REVIEW` is in Doina's L530 list but
        **not** in the canonical `status` enum (Q6/Q33). So the payment holds at AUTHORISED
        and never reaches APPROVED. Inventing the state is defect 2026-04-24.
        """
        return self.decision == REVIEW


def assess(
    *,
    amount: float,
    wire_type: Optional[str],
    purpose_code: Optional[str],
    high_risk_purpose_codes: frozenset,
    prior_payments_to_beneficiary: int,
    debtor_payments_in_window: int,
    is_external_creditor: bool,
) -> FraudAssessment:
    """Run every rule, sum the weights, map to a decision. Total and deterministic."""
    outcomes = [
        _amount_tier(amount),
        _cross_border(wire_type),
        _new_beneficiary(prior_payments_to_beneficiary, is_external_creditor),
        _velocity(debtor_payments_in_window),
        _purpose_code_risk(purpose_code, high_risk_purpose_codes),
    ]

    score = MODEL_BASELINE_SCORE + sum(o.weight for o in outcomes if o.fired)
    score = max(SCORE_MIN, min(SCORE_MAX, score))

    if score >= DECLINE_THRESHOLD:
        decision = DECLINED
    elif score >= REVIEW_THRESHOLD:
        decision = REVIEW
    else:
        decision = APPROVED

    return FraudAssessment(
        score=score,
        decision=decision,
        rules_fired=[o.name for o in outcomes if o.fired],
        outcomes=outcomes,
    )


def describe(assessment: FraudAssessment) -> str:
    """One line for a `checks[]` detail. Names the rules, not just the number."""
    fired = ", ".join(assessment.rules_fired) or "no rules"
    return (
        f"Score {assessment.score}/100 ({fired} fired; baseline "
        f"{MODEL_BASELINE_SCORE} from {MODEL_ID} {MODEL_VERSION}, SIMULATED). "
        f"REVIEW at {REVIEW_THRESHOLD}, DECLINE at {DECLINE_THRESHOLD}."
    )


# --------------------------------------------------------------------------- #

def _amount_tier(amount: float) -> RuleOutcome:
    for threshold, weight, label in _AMOUNT_BANDS:
        if amount >= threshold:
            return RuleOutcome(
                "AMOUNT_TIER", True, weight,
                f"{amount:,.2f} is {label}.",
            )
    return RuleOutcome(
        "AMOUNT_TIER", False, 0,
        f"{amount:,.2f} is below the lowest scored band ({_AMOUNT_BANDS[-1][0]:,}).",
    )


def _cross_border(wire_type: Optional[str]) -> RuleOutcome:
    if (wire_type or "").upper() == "INTERNATIONAL":
        return RuleOutcome(
            "CROSS_BORDER", True, _CROSS_BORDER_WEIGHT,
            "Cross-border payment — more parties, less recourse.",
        )
    return RuleOutcome("CROSS_BORDER", False, 0, "Domestic or intrabank payment.")


def _new_beneficiary(prior: int, is_external_creditor: bool) -> RuleOutcome:
    """Beneficiary novelty, off the same query shape stage 3's duplicate detection uses.

    Only meaningful for an external beneficiary: an internal transfer between two accounts
    we hold has no "new payee" risk worth scoring.
    """
    if not is_external_creditor:
        return RuleOutcome(
            "NEW_BENEFICIARY", False, 0,
            "Beneficiary is held at Leafy Bank — payee novelty does not apply.",
        )
    if prior == 0:
        return RuleOutcome(
            "NEW_BENEFICIARY", True, _NEW_BENEFICIARY_WEIGHT,
            "First payment this debtor has ever sent to this beneficiary.",
        )
    return RuleOutcome(
        "NEW_BENEFICIARY", False, 0,
        f"{prior} prior payment(s) to this beneficiary.",
    )


def _velocity(count: int) -> RuleOutcome:
    if count >= VELOCITY_THRESHOLD:
        return RuleOutcome(
            "VELOCITY", True, _VELOCITY_WEIGHT,
            f"{count} payments from this debtor inside the velocity window "
            f"(threshold {VELOCITY_THRESHOLD}).",
        )
    return RuleOutcome(
        "VELOCITY", False, 0,
        f"{count} payment(s) from this debtor inside the velocity window — below the "
        f"threshold of {VELOCITY_THRESHOLD}.",
    )


def _purpose_code_risk(
    purpose_code: Optional[str], high_risk: frozenset
) -> RuleOutcome:
    """Her L446: the purpose code feeds *"AML risk scoring (Stage 4)"*, not just free text.

    Fed by stage 3's resolved `remittance.purposeCode`, which is why stage 3 resolving it
    against the real `purposeCodes` collection mattered.
    """
    if not purpose_code:
        return RuleOutcome(
            "PURPOSE_CODE_RISK", False, 0,
            "No purpose code was resolved for this payment.",
        )
    if purpose_code.upper() in high_risk:
        return RuleOutcome(
            "PURPOSE_CODE_RISK", True, _HIGH_RISK_PURPOSE_WEIGHT,
            f"Purpose code {purpose_code.upper()} is AML-relevant.",
        )
    return RuleOutcome(
        "PURPOSE_CODE_RISK", False, 0,
        f"Purpose code {purpose_code.upper()} carries no elevated AML risk.",
    )


# --- query shapes -------------------------------------------------------------
# Two rules need counts from `payments`. The filters are built here and executed by the
# caller, exactly as stage 3's `duplicate_detection.recent_duplicate_filter` does: the
# predicate stays unit-testable without a database, and the I/O stays in the stage.

# The velocity window. A separate concept from duplicate detection's 15 minutes — that one
# asks "is this the same payment twice?", this one asks "is this account unusually busy?".
VELOCITY_WINDOW_SECONDS_DEFAULT = 3600


def velocity_window_seconds() -> int:
    """Read lazily, per defects.md 2026-06-30: a module-level `os.getenv` is evaluated
    before `load_dotenv` runs in `main.py` and silently takes the default."""
    import os

    raw = os.getenv("FRAUD_VELOCITY_WINDOW_SECONDS")
    try:
        return int(raw) if raw else VELOCITY_WINDOW_SECONDS_DEFAULT
    except ValueError:
        return VELOCITY_WINDOW_SECONDS_DEFAULT


def velocity_filter(*, debtor_account_id: str, now, exclude_payment_id: str) -> dict:
    """How many payments has this debtor account sent inside the window?

    Excludes the payment being scored — stage 1 persists at DRAFT before stage 4 runs, so
    without this every payment counts itself and the threshold is effectively one lower.
    Leads on `debtor.accountId`, which `idx_payments_debtorAccount_status` covers; no new
    index (measuring first — defect 2026-07-08).
    """
    from datetime import timedelta

    return {
        "debtor.accountId": debtor_account_id,
        "paymentId": {"$ne": exclude_payment_id},
        "createdAt": {"$gte": now - timedelta(seconds=velocity_window_seconds())},
    }


def beneficiary_history_filter(
    *, debtor_account_id: str, creditor_account_no, exclude_payment_id: str
) -> dict:
    """Has this debtor ever paid this beneficiary before? No time window, by design.

    Novelty is a lifetime question: a beneficiary first paid two years ago is not new. That
    is the one difference from the duplicate filter, which is entirely about a short window.
    Terminal failures still count as history — an attempt tells you the payee is known to
    the customer, even if it did not settle.
    """
    return {
        "debtor.accountId": debtor_account_id,
        "creditor.accountNo": creditor_account_no,
        "paymentId": {"$ne": exclude_payment_id},
    }
