"""What the Reconciliation Agent is allowed to do — enforced in code, not in the prompt.

Defect 2026-09-28 A2 (`prompt-as-enforcement`): a constraint stated only in a system prompt
is not enforced. Every rule here is checked at the propose boundary (the agent's tool call)
and again at the execute boundary (after operator approval), and the owning service's own
guards (`_LEGAL`, the chargeBearer gate) still run behind both.

The agent classifies a `cause`; the table maps (category, cause) to the actions it may take.
`chargeBearer` is read from the payment, never from the model, so for a FEE the agent cannot
choose the books (parent plan Decision 2).
"""

from __future__ import annotations

from typing import Optional

CATEGORY_DISCREPANCY = "RECONCILIATION_DISCREPANCY"
CATEGORY_MISSING = "RECONCILIATION_MISSING"
CATEGORY_ORPHANED = "ORPHANED_SETTLEMENT"
WATCHED_CATEGORIES = (CATEGORY_DISCREPANCY, CATEGORY_MISSING, CATEGORY_ORPHANED)

TIMING_LAG = "TIMING_LAG"
REFERENCE_MISMATCH = "REFERENCE_MISMATCH"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
FEE = "FEE"
ORPHANED = "ORPHANED"
CAUSES = (TIMING_LAG, REFERENCE_MISMATCH, AMOUNT_MISMATCH, FEE, ORPHANED)

RECHECK = "RECHECK"
LINK = "LINK_STATEMENT_ENTRY"
POST_ADJUSTMENT = "POST_ADJUSTMENT"
ACCEPT = "ACCEPT_DISCREPANCY"
ESCALATE = "ESCALATE_TO_CORRESPONDENT"
DISMISS = "DISMISS"

# RECHECK is the one action the agent takes on its own: it resolves nothing unless the
# deterministic engine then says RECONCILED. Everything else waits for operator approval.
AUTONOMOUS_ACTIONS = frozenset({RECHECK})

# Every set here must stay inside what the services accept — transactions `_LEGAL`
# (payments_service.py) plus the ledger's RECHECK/LINK gates (resolution_service.py).
# `test_policy.py` pins that.
_ALLOWED = {
    CATEGORY_MISSING: {
        TIMING_LAG: {RECHECK, ESCALATE},
        REFERENCE_MISMATCH: {LINK, ESCALATE},
    },
    CATEGORY_DISCREPANCY: {
        FEE: None,  # chargeBearer-dependent, see allowed_actions
        # A transposed amount clears only when the correspondent sends a corrected line, so
        # the agent may ask for one and re-check — never accept or post a number it doubts.
        AMOUNT_MISMATCH: {ESCALATE, RECHECK},
    },
    CATEGORY_ORPHANED: {
        REFERENCE_MISMATCH: {LINK, ESCALATE},
        ORPHANED: {ESCALATE, DISMISS},
    },
}

# How far the agent may defer its own next look, and how many times before it must escalate.
MAX_RECHECK_MINUTES = 10
MAX_RECHECKS = 3


def allowed_actions(category: str, cause: str, charge_bearer: Optional[str]) -> frozenset:
    by_cause = _ALLOWED.get(category) or {}
    if cause not in by_cause:
        return frozenset()
    if category == CATEGORY_DISCREPANCY and cause == FEE:
        return frozenset({POST_ADJUSTMENT} if charge_bearer == "DEBT" else {ACCEPT})
    return frozenset(by_cause[cause])


def fee_cause_refusal(discrepancy_amount: Optional[float],
                      known_charges: Optional[list]) -> Optional[str]:
    """Why a FEE cause is inadmissible, or None when it is grounded in a levied charge.

    A fee explanation is only admissible when the discrepancy equals a charge the
    correspondent actually levies — otherwise the model is rationalising an unknown
    delta as a fee (defect 2026-10-03, R4: 54.0 explained as "fee + extra correspondent
    charge" and proposed for acceptance). Any other delta is an AMOUNT_MISMATCH, whose
    permitted actions never move money.
    """
    if not known_charges or discrepancy_amount is None:
        return None
    amount = round(abs(float(discrepancy_amount)), 2)
    charges = sorted({round(abs(float(c)), 2) for c in known_charges})
    if amount in charges:
        return None
    permitted = sorted(_ALLOWED[CATEGORY_DISCREPANCY][AMOUNT_MISMATCH])
    return (f"The discrepancy {amount} matches no charge this correspondent levies "
            f"({charges}); it is not a fee. Record AMOUNT_MISMATCH instead "
            f"(permitted: {permitted}).")


def check_proposal(*, category: str, cause: Optional[str], charge_bearer: Optional[str],
                   action: str, params: dict, candidates: list,
                   discrepancy_amount: Optional[float],
                   known_charges: Optional[list] = None) -> Optional[str]:
    """None when the proposal is permitted, otherwise the reason it is refused."""
    if cause not in CAUSES:
        return "Record the investigation (with a cause) before proposing an action."
    if category == CATEGORY_DISCREPANCY and cause == FEE:
        grounded = fee_cause_refusal(discrepancy_amount, known_charges)
        if grounded:
            return grounded
    allowed = allowed_actions(category, cause, charge_bearer)
    if action not in allowed:
        return (f"{action} is not permitted for a {category} with cause {cause} "
                f"(chargeBearer {charge_bearer}); permitted: {sorted(allowed) or 'none'}.")
    if category == CATEGORY_ORPHANED and action == DISMISS and candidates:
        # Defect 2026-10-05 (R2): a re-keyed line was dismissed as a misposting while its
        # payment sat MISSING. A line with a possible owner is linked or escalated, never dropped.
        return ("DISMISS is refused: this line has candidate payments "
                f"({len(candidates)}). Record REFERENCE_MISMATCH and propose LINK, or ESCALATE.")
    if action == POST_ADJUSTMENT:
        proposed = params.get("amount")
        if discrepancy_amount is None or proposed is None or \
                round(float(proposed), 2) != round(float(discrepancy_amount), 2):
            return (f"The adjustment must equal the discrepancy exactly "
                    f"({discrepancy_amount}); proposed {proposed}.")
    if action == LINK:
        idx = params.get("candidateIndex")
        if not isinstance(idx, int) or not 0 <= idx < len(candidates or []):
            return ("LINK must name a candidate returned by find_statement_candidates "
                    f"(index 0..{len(candidates or []) - 1}); got {idx!r}.")
    return None


def capped_recheck_minutes(minutes) -> int:
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        m = MAX_RECHECK_MINUTES
    return max(1, min(m, MAX_RECHECK_MINUTES))
