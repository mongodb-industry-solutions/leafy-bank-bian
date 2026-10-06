"""What the Cutoff Agent may do — enforced in code, not in the prompt (defect A2).

Autonomous actions nudge a blocker and never change a payment's state. The three gated
actions change the value date or the path and wait for a human at `interrupt()`. Every gated
proposal is checked here at the propose boundary and again at execute; the transactions
routes keep their own guards behind both (untagged → 400; EXPEDITE past the external cut-off
or with any OPEN exception → refused).
"""

from __future__ import annotations

from typing import Optional

import cutoff_risk as risk_mod
from cutoff_window import AFTER_INTERNAL

SEND_APPROVAL_REMINDER = "SEND_APPROVAL_REMINDER"
ESCALATE_TO_BACKUP_APPROVER = "ESCALATE_TO_BACKUP_APPROVER"
RAISE_SCREENING_PRIORITY = "RAISE_SCREENING_PRIORITY"
NOTIFY_CUSTOMER_FUNDS = "NOTIFY_CUSTOMER_FUNDS"
RECORD_ASSESSMENT = "RECORD_ASSESSMENT"

HOLD_NEXT_VALUE_DATE = "HOLD_NEXT_VALUE_DATE"
EXPEDITE = "EXPEDITE"
DEFER_NEXT_BUSINESS_DAY = "DEFER_NEXT_BUSINESS_DAY"

AUTO = frozenset({SEND_APPROVAL_REMINDER, ESCALATE_TO_BACKUP_APPROVER,
                  RAISE_SCREENING_PRIORITY, NOTIFY_CUSTOMER_FUNDS, RECORD_ASSESSMENT})
GATED = frozenset({HOLD_NEXT_VALUE_DATE, EXPEDITE, DEFER_NEXT_BUSINESS_DAY})

# How many times each autonomous remedy may run on one case.
CAPS = {
    SEND_APPROVAL_REMINDER: 2,
    ESCALATE_TO_BACKUP_APPROVER: 1,
    RAISE_SCREENING_PRIORITY: 1,
    NOTIFY_CUSTOMER_FUNDS: 1,
}

# The transactions route each gated action calls (main.py:656-669).
ROUTE_FOR = {
    HOLD_NEXT_VALUE_DATE: {"path": "/PaymentOrderProcedure/HoldNextValueDate", "body": {}},
    EXPEDITE: {"path": "/PaymentOrderProcedure/CutoffDecision",
               "body": {"decision": "EXPEDITE"}},
    DEFER_NEXT_BUSINESS_DAY: {"path": "/PaymentOrderProcedure/CutoffDecision",
                              "body": {"decision": "NEXT_VALUE_DATE"}},
}


def _under_cap(action: str, taken: dict) -> bool:
    return int((taken or {}).get(action) or 0) < CAPS[action]


def allowed_auto(*, risk: Optional[dict], status: str, taken: Optional[dict] = None,
                 backup_on_shift: bool = False,
                 screening_ahead: Optional[int] = None, approval_open: bool = True,
                 screening_queued: bool = True) -> frozenset:
    """The autonomous actions permitted now. ON_TRACK (or no window) allows only the record.

    A remedy whose target is missing (no OPEN approval request, no queued screening item) is
    not offered, so it never counts as untried and never blocks a HOLD."""
    if risk is None or risk.get("riskLevel") == risk_mod.ON_TRACK:
        return frozenset({RECORD_ASSESSMENT})
    taken = taken or {}
    allowed = {RECORD_ASSESSMENT}
    if status == risk_mod.PENDING_APPROVAL and approval_open:
        if _under_cap(SEND_APPROVAL_REMINDER, taken):
            allowed.add(SEND_APPROVAL_REMINDER)
        if backup_on_shift and _under_cap(ESCALATE_TO_BACKUP_APPROVER, taken):
            allowed.add(ESCALATE_TO_BACKUP_APPROVER)
    elif status == risk_mod.PENDING_SCREENING and screening_queued:
        if (screening_ahead is None or screening_ahead > 0) and \
                _under_cap(RAISE_SCREENING_PRIORITY, taken):
            allowed.add(RAISE_SCREENING_PRIORITY)
    elif status == risk_mod.PENDING_FUNDS:
        if _under_cap(NOTIFY_CUSTOMER_FUNDS, taken):
            allowed.add(NOTIFY_CUSTOMER_FUNDS)
    return frozenset(allowed)


def untried_remedies(**kwargs) -> frozenset:
    """Autonomous remedies still available — `allowed_auto` without the record."""
    return allowed_auto(**kwargs) - {RECORD_ASSESSMENT}


def check_proposal(action: str, *, status: str, risk: Optional[dict],
                   open_exceptions: int = 0, untried: frozenset = frozenset(),
                   rejected: Optional[list] = None) -> Optional[str]:
    """None when the gated proposal is permitted, otherwise the reason it is refused."""
    if action not in GATED:
        return f"{action} is not a gated action; permitted: {sorted(GATED)}."
    if risk is None:
        return "No cut-off window applies to this payment."
    level, current = risk.get("riskLevel"), risk.get("phase")
    for r in rejected or []:
        if r.get("action") == action and r.get("phase") == current:
            return f"{action} was rejected in phase {current}; it cannot be proposed again."

    if action == HOLD_NEXT_VALUE_DATE:
        if status not in risk_mod.PENDING_HOLDS:
            return f"HOLD_NEXT_VALUE_DATE needs a PENDING_* hold, not {status}."
        if level != risk_mod.WILL_MISS:
            return f"HOLD_NEXT_VALUE_DATE needs risk WILL_MISS, not {level}."
        if untried:
            return f"Try the autonomous remedies first: {sorted(untried)}."
        return None

    if status != risk_mod.CUTOFF_EXCEPTION:
        return f"{action} needs a CUTOFF_EXCEPTION hold, not {status}."
    if action == EXPEDITE:
        if current != AFTER_INTERNAL:
            return f"EXPEDITE needs phase AFTER_INTERNAL, not {current}."
        if level == risk_mod.WILL_MISS:
            return "EXPEDITE is refused: the payment will miss the external cut-off."
        if open_exceptions:
            return f"EXPEDITE is refused: {open_exceptions} open exception(s) on the payment."
        return None
    # DEFER_NEXT_BUSINESS_DAY
    if level != risk_mod.WILL_MISS and not open_exceptions:
        return "DEFER needs risk WILL_MISS or an open exception; propose EXPEDITE instead."
    return None
