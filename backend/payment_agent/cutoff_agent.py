"""The Cutoff Agent — watches a tagged, held wire against today's cut-offs, nudges whatever is
blocking it, and proposes the one change of value date or path the policy permits.

Woken (C3: the worker) for payments with `demo.clockRunId` held at PENDING_APPROVAL,
PENDING_FUNDS, PENDING_SCREENING or CUTOFF_EXCEPTION. The deterministic core decides the
numbers: `cutoff_risk` the risk level, `cutoff_rules` what is permitted. The agent's job is the
reasoning: read the blocker, try the autonomous remedies, record a grounded assessment, and
propose a gated action only when one is permitted.

## What the agent may do

- **On its own** (`cutoff_rules.AUTO`): send an approval reminder, escalate to the backup
  approver, raise the screening priority, notify the customer of a funds shortfall, record its
  assessment. Each is checked against a freshly computed risk and its cap.
- **With approval** (`cutoff_rules.GATED`): HOLD_NEXT_VALUE_DATE, EXPEDITE and
  DEFER_NEXT_BUSINESS_DAY. `propose_resolution` checks the proposal; the graph pauses at
  `approval` (`interrupt()`); a human resumes it. `execute` re-checks against the live payment
  and risk (the clock may have passed 18:45, an exception may have opened) and calls the
  transactions route. `verify` closes the case only when the payment shows the decision.

## Write surface

The agent writes its own case (`cutoffCases`) and, for the autonomous remedies, only named
fields of transactions-owned views (approved Q5): `approvalRequests.reminders`,
`approvalRequests.escalatedTo/assignedTo`, `screeningQueue.priority`, and a new
`notifications` row. It never writes `payments`. Case and payment ids reach the tools through
`InjectedState`, never from the model's arguments. `thread_id = caseId`.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional, TypedDict

from bson import ObjectId
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import InjectedState, ToolNode
from langgraph.types import interrupt
from typing_extensions import Annotated

import clients
import cutoff_cases as cases
import cutoff_clock
import cutoff_evidence as ev
import cutoff_risk as risk_mod
import cutoff_rules as rules
import cutoff_window as cw
import transactions_client
# Shared with the Reconciliation Agent (approved Q7): the step timeline, the interrupt-based
# pause check, the thread reader and the tag-leak cleaner are thread-generic.
from reconciliation_agent import (  # noqa: F401 — re-exported for the C3 routes
    _clean_text, is_awaiting_approval, messages_to_steps, thread_messages)

logger = logging.getLogger(__name__)

ACTOR = "cutoff-agent"
APPROVE = "APPROVE"
REJECT = "REJECT"
SUPERSEDED = "SUPERSEDED"

VERIFIED = "VERIFIED"
VERIFIED_REFUSED = "REFUSED"
VERIFIED_NOT_APPLIED = "NOT_APPLIED"

KIND_AUTONOMOUS = "AUTONOMOUS"
KIND_NEEDS_APPROVAL = "NEEDS_APPROVAL"
KIND_RESOLVE_BLOCKER = "RESOLVE_BLOCKER"
KIND_NONE = "NONE"
_KINDS = {KIND_AUTONOMOUS, KIND_NEEDS_APPROVAL, KIND_RESOLVE_BLOCKER, KIND_NONE}
_CONFIDENCE_VALUES = {"HIGH", "MEDIUM", "LOW"}

_SUBMITTED_OR_LATER = {"SUBMITTED", "IN_PROGRESS", "POSTED", "SETTLED", "RECONCILED"}

# ToolNode runs one model turn's tool calls on a thread pool, so two remedies could both pass
# the cap check before either records. Holding this lock from check to record makes the cap
# exact within a process; the case lease keeps a second process's run off the same case.
_AUTO_LOCK = threading.Lock()

CUTOFF_SYSTEM_PROMPT = """\
You are the Cutoff Agent for Leafy Bank. A tagged wire is held (PENDING_APPROVAL, \
PENDING_FUNDS, PENDING_SCREENING or CUTOFF_EXCEPTION) and today's Fedwire cut-off is \
approaching. Work out whether it will make today, nudge whatever is blocking it, and propose \
a change of value date or path only when the policy permits one.

Steps:
1. `get_payment_context` and `get_cutoff_window` (the computed phase, risk level, blocker and \
the actions permitted right now).
2. Evidence for the blocker: `get_approval_status`, `get_screening_queue` or \
`get_funds_position`; `get_stage_timing` for how long the rest takes; `get_open_exceptions`.
3. Autonomous remedies, as permitted: `send_approval_reminder`, \
`escalate_to_backup_approver` (only when the backup is on shift), `raise_screening_priority`, \
`notify_customer_funds`.
4. `record_assessment`: your diagnosis, the blocker (it must equal the computed blockerType), \
evidence citing specific records and minutes, a confidence and a recommendation \
{action, kind}. kind is AUTONOMOUS, NEEDS_APPROVAL, RESOLVE_BLOCKER (a human must clear the \
blocker; nothing to propose) or NONE (on track).
5. Only if a gated action is permitted: `propose_resolution` with HOLD_NEXT_VALUE_DATE, \
EXPEDITE or DEFER_NEXT_BUSINESS_DAY. A human approves it.

Rules:
- The code decides what is permitted; a refusal explains why. Do not argue with it.
- An ON_TRACK payment gets an assessment with recommendation NONE and nothing else.
- End with a two-sentence summary for the operator.
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ids(state: dict) -> tuple[str, str]:
    return state["case_id"], state["payment_id"]


def _action_record(action: str, result: str, detail: Any = None) -> dict:
    return {"action": action, "at": _now().isoformat(), "result": result, "detail": detail}


def _minutes_since(at: datetime, since: Optional[datetime]) -> float:
    if since is None:
        return 0.0
    since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    return max(0.0, (at - since).total_seconds() / 60)


def _snapshot(db: Any, payment_id: str) -> dict:
    """The payment, business time, evidence and risk as they stand right now.

    `actionable` is False for an untagged payment, a payment no longer in a cut-off hold, or a
    run whose clock is gone — the agent may then read but never act.
    """
    payment = db["payments"].find_one({"paymentId": payment_id}, {"_id": 0}) or {}
    status = payment.get("status")
    run_id = (payment.get("demo") or {}).get("clockRunId")
    at = cutoff_clock.now(db, run_id)
    snap: dict = {"payment": payment, "status": status, "runId": run_id, "at": at,
                  "risk": None, "timing": None, "approval": None, "screening": None,
                  "funds": None, "exceptions": {"count": 0, "categories": []},
                  "actionable": False}
    if not payment or at is None or status not in risk_mod.BLOCKER_FOR:
        return snap
    run_doc = cutoff_clock.run(db, run_id)
    snap["exceptions"] = ev.open_exceptions(db, payment_id)
    snap["timing"] = ev.stage_timing(db, rail=payment.get("rail"), status=status, at=at)
    if status == risk_mod.PENDING_APPROVAL:
        snap["approval"] = ev.approval_status(db, payment_id=payment_id, at=at)
    elif status == risk_mod.PENDING_SCREENING:
        snap["screening"] = ev.screening_status(db, payment_id=payment_id, at=at)
    elif status == risk_mod.PENDING_FUNDS:
        snap["funds"] = ev.funds_position(db, payment,
                                          business_date=cutoff_clock.business_date(run_doc, at))
    timing = snap["timing"]
    snap["risk"] = risk_mod.assess(
        status=status, window=cw.window_for_payment(payment), at=at,
        minutes_in_stage=_minutes_since(at, (payment.get("lifecycle") or {}).get("stateEnteredAt")),
        remaining_p50_min=timing.get("remainingP50Min"),
        remaining_p90_min=timing.get("remainingP90Min"),
        stage_p50_min=timing.get("stageP50Min"), stage_p90_min=timing.get("stageP90Min"),
        screening=snap["screening"], funds=snap["funds"],
        open_exceptions=snap["exceptions"]["count"])
    snap["actionable"] = snap["risk"] is not None
    return snap


def _taken(case: dict) -> dict:
    counts: dict = {}
    for a in (case.get("agent") or {}).get("actionsTaken") or []:
        if a.get("result") == "DONE":
            counts[a["action"]] = counts.get(a["action"], 0) + 1
    return counts


def _auto_kwargs(snap: dict, case: dict) -> dict:
    approval = snap.get("approval") or {}
    backup = approval.get("backup") or {}
    return {"risk": snap["risk"], "status": snap["status"], "taken": _taken(case),
            "backup_on_shift": bool(backup.get("onShift")),
            "screening_ahead": (snap.get("screening") or {}).get("ahead"),
            "approval_open": bool(approval.get("open")),
            "screening_queued": snap.get("screening") is not None}


def _not_actionable(snap: dict) -> Optional[str]:
    if not snap["payment"]:
        return "Payment not found."
    if snap["at"] is None:
        return "The demo clock run is gone; this case has expired."
    if not snap["actionable"]:
        return (f"Payment is {snap['status']} with no cut-off window in play; "
                "there is nothing to act on.")
    return None


def _auto_refusal(db: Any, case_id: str, payment_id: str, action: str) -> tuple[Optional[str], dict]:
    """Policy for an autonomous remedy, against a freshly computed risk."""
    snap = _snapshot(db, payment_id)
    refusal = _not_actionable(snap)
    if refusal:
        return refusal, snap
    allowed = rules.allowed_auto(**_auto_kwargs(snap, cases.get(db, case_id)))
    if action not in allowed:
        return (f"{action} is not permitted now (risk {snap['risk']['riskLevel']}, status "
                f"{snap['status']}); permitted: {sorted(allowed)}."), snap
    return None, snap


def _gated_refusal(db: Any, case_id: str, payment_id: str, action: str) -> tuple[Optional[str], dict]:
    """Policy for a gated proposal, against the live payment and risk. Used at propose and
    again at execute."""
    snap = _snapshot(db, payment_id)
    refusal = _not_actionable(snap)
    if refusal:
        return refusal, snap
    case = cases.get(db, case_id)
    return rules.check_proposal(
        action, status=snap["status"], risk=snap["risk"],
        open_exceptions=snap["exceptions"]["count"],
        untried=rules.untried_remedies(**_auto_kwargs(snap, case)),
        rejected=(case.get("agent") or {}).get("rejectedActions") or [],
    ), snap


def _permitted_gated(db: Any, case_id: str, payment_id: str) -> list:
    return sorted(a for a in rules.GATED if _gated_refusal(db, case_id, payment_id, a)[0] is None)


def _risk_view(risk: Optional[dict]) -> Optional[dict]:
    if risk is None:
        return None
    keys = ("phase", "riskLevel", "blockerType", "wireType", "minutesToInternal",
            "minutesToExternal", "internalCutoffAt", "externalCutoffAt", "remainingP50Min",
            "remainingP90Min", "queueMinutes", "openExceptions", "readiness", "reasons")
    return {k: risk.get(k) for k in keys}


def _record_auto(db: Any, case_id: str, action: str, result: Any, detail: dict) -> bool:
    """Record an autonomous remedy: DONE only when its write hit a target, else REFUSED (a
    REFUSED entry does not count against the cap)."""
    done = result.matched_count > 0
    cases.set_agent_fields(db, case_id, {}, push={
        "actionsTaken": _action_record(action, "DONE" if done else VERIFIED_REFUSED,
                                       detail if done else "No OPEN target matched.")})
    return done


def _build_tools(db: Any):
    """The agent's tools. Ids come from graph state (`InjectedState`), not from the model."""

    @tool
    def get_payment_context(state: Annotated[dict, InjectedState]) -> str:
        """The held payment: status, rail, wire type, amount, debtor, the demo run and the \
current business time."""
        _, payment_id = _ids(state)
        snap = _snapshot(db, payment_id)
        p = snap["payment"]
        if not p:
            return f"No payment {payment_id}."
        return json.dumps({
            "paymentId": payment_id, "status": snap["status"], "rail": p.get("rail"),
            "wireType": (p.get("wireDetails") or {}).get("wireType"),
            "amount": p.get("instructedAmount"),
            "currency": p.get("instructedCurrency") or p.get("currency"),
            "debtorAccountId": (p.get("debtor") or {}).get("accountId"),
            "stateEnteredAt": (p.get("lifecycle") or {}).get("stateEnteredAt"),
            "cutoff": p.get("cutoff"), "clockRunId": snap["runId"], "businessNow": snap["at"],
        }, default=str)

    @tool
    def get_cutoff_window(state: Annotated[dict, InjectedState]) -> str:
        """The computed cut-off phase, risk level, blocker, minutes to each cut-off, and the \
autonomous and gated actions permitted right now."""
        case_id, payment_id = _ids(state)
        snap = _snapshot(db, payment_id)
        refusal = _not_actionable(snap)
        if refusal:
            return refusal
        case = cases.get(db, case_id)
        return json.dumps({
            "risk": _risk_view(snap["risk"]),
            "autonomousPermitted": sorted(rules.allowed_auto(**_auto_kwargs(snap, case))),
            "gatedPermitted": _permitted_gated(db, case_id, payment_id),
        }, default=str)

    @tool
    def get_stage_timing(state: Annotated[dict, InjectedState]) -> str:
        """Historical p50/p90 minutes for the current stage and the rest of the journey \
(same rail, last 14 days, same time of day), with sample counts and the SEED/LIVE mix."""
        _, payment_id = _ids(state)
        return json.dumps(_snapshot(db, payment_id)["timing"] or "No timing: not in a hold.",
                          default=str)

    @tool
    def get_approval_status(state: Annotated[dict, InjectedState]) -> str:
        """The OPEN approval request: how long it has been open, reminders sent, and the \
primary and backup approvers with on-shift status and workload."""
        _, payment_id = _ids(state)
        snap = _snapshot(db, payment_id)
        if snap["at"] is None:
            return "The demo clock run is gone."
        return json.dumps(ev.approval_status(db, payment_id=payment_id, at=snap["at"]), default=str)

    @tool
    def get_screening_queue(state: Annotated[dict, InjectedState]) -> str:
        """This payment's place in the screening queue: position, items ahead, analysts on \
shift, its priority and the run's highest priority."""
        _, payment_id = _ids(state)
        snap = _snapshot(db, payment_id)
        if snap["at"] is None:
            return "The demo clock run is gone."
        out = ev.screening_status(db, payment_id=payment_id, at=snap["at"])
        return json.dumps(out or "Not in the screening queue.", default=str)

    @tool
    def get_funds_position(state: Annotated[dict, InjectedState]) -> str:
        """The debtor's available balance, the shortfall against this payment, and any \
expected credits with their expected time today."""
        _, payment_id = _ids(state)
        snap = _snapshot(db, payment_id)
        if snap["at"] is None or not snap["payment"]:
            return "No payment or demo clock run."
        day = cutoff_clock.business_date(cutoff_clock.run(db, snap["runId"]), snap["at"])
        return json.dumps(ev.funds_position(db, snap["payment"], business_date=day), default=str)

    @tool
    def get_open_exceptions(state: Annotated[dict, InjectedState]) -> str:
        """OPEN exceptions on this payment (count and categories). Any one blocks EXPEDITE."""
        _, payment_id = _ids(state)
        return json.dumps(ev.open_exceptions(db, payment_id), default=str)

    @tool
    def send_approval_reminder(state: Annotated[dict, InjectedState]) -> str:
        """Remind the assigned approver of the OPEN approval request. No approval needed; \
capped per case."""
        case_id, payment_id = _ids(state)
        action = rules.SEND_APPROVAL_REMINDER
        with _AUTO_LOCK:
            refusal, snap = _auto_refusal(db, case_id, payment_id, action)
            if refusal:
                return f"Refused: {refusal}"
            to = (snap["approval"] or {}).get("assignedTo")
            result = db[ev.APPROVAL_REQUESTS].update_one(
                {"paymentId": payment_id, "status": ev.OPEN},
                {"$push": {"reminders": {"at": snap["at"], "by": ACTOR, "caseId": case_id,
                                         "to": to}}})
            if not _record_auto(db, case_id, action, result, {"to": to}):
                return "Refused: no OPEN approval request."
        return f"Reminder sent to {to}."

    @tool
    def escalate_to_backup_approver(state: Annotated[dict, InjectedState]) -> str:
        """Reassign the OPEN approval request to the primary approver's backup. Only when \
the backup is on shift; once per case."""
        case_id, payment_id = _ids(state)
        action = rules.ESCALATE_TO_BACKUP_APPROVER
        with _AUTO_LOCK:
            refusal, snap = _auto_refusal(db, case_id, payment_id, action)
            if refusal:
                return f"Refused: {refusal}"
            backup = ((snap["approval"] or {}).get("backup") or {}).get("staffId")
            result = db[ev.APPROVAL_REQUESTS].update_one(
                {"paymentId": payment_id, "status": ev.OPEN},
                {"$set": {"escalatedTo": backup, "assignedTo": backup}})
            if not _record_auto(db, case_id, action, result, {"to": backup}):
                return "Refused: no OPEN approval request."
        return f"Escalated to backup approver {backup}."

    @tool
    def raise_screening_priority(state: Annotated[dict, InjectedState]) -> str:
        """Move this payment to the front of the screening queue (the run's highest \
priority + 1). Once per case."""
        case_id, payment_id = _ids(state)
        action = rules.RAISE_SCREENING_PRIORITY
        with _AUTO_LOCK:
            refusal, snap = _auto_refusal(db, case_id, payment_id, action)
            if refusal:
                return f"Refused: {refusal}"
            priority = int((snap["screening"] or {}).get("runMaxPriority") or 0) + 1
            result = db[ev.SCREENING_QUEUE].update_one(
                {"paymentId": payment_id, "status": ev.OPEN}, {"$set": {"priority": priority}})
            if not _record_auto(db, case_id, action, result, {"priority": priority}):
                return "Refused: no OPEN screening item."
        after = ev.screening_status(db, payment_id=payment_id, at=snap["at"]) or {}
        return f"Screening priority raised to {priority}; now position {after.get('position')}."

    @tool
    def notify_customer_funds(state: Annotated[dict, InjectedState]) -> str:
        """Tell the customer the account is short for this wire and by how much. Once per \
case."""
        case_id, payment_id = _ids(state)
        action = rules.NOTIFY_CUSTOMER_FUNDS
        with _AUTO_LOCK:
            refusal, snap = _auto_refusal(db, case_id, payment_id, action)
            if refusal:
                return f"Refused: {refusal}"
            payment, funds = snap["payment"], snap["funds"] or {}
            oid = ObjectId()
            currency = payment.get("instructedCurrency") or payment.get("currency") or "USD"
            db["notifications"].insert_one({
                "_id": oid,
                "notificationId": f"NOTIF-{oid}",
                "eventType": "CutoffFundsShortfall",
                "message": (f"Your wire {payment_id} is short by {currency} "
                            f"{funds.get('shortfall')} and may miss today's cut-off. Add funds "
                            "or it will go on the next business day."),
                "notificationDate": snap["at"],
                "recipient": {"customerId": payment.get("customerId")},
                "paymentId": payment_id,
                "sourceSystem": ACTOR,
                "caseId": case_id,
            })
            cases.set_agent_fields(db, case_id, {}, push={
                "actionsTaken": _action_record(action, "DONE", {"notificationId": f"NOTIF-{oid}"})})
        return f"Customer notified (NOTIF-{oid}) of a {funds.get('shortfall')} shortfall."

    @tool
    def record_assessment(
        state: Annotated[dict, InjectedState],
        diagnosis: str,
        blocker: str,
        evidence: list[str],
        confidence: str,
        recommendation_kind: str,
        rationale: str,
        recommendation_action: Optional[str] = None,
        reasoning: str = "",
    ) -> str:
        """Record your assessment. `blocker` must equal the computed blockerType (APPROVAL, \
FUNDS, SCREENING or CUTOFF_DECISION). `confidence` is HIGH, MEDIUM or LOW. \
`recommendation_kind` is AUTONOMOUS, NEEDS_APPROVAL, RESOLVE_BLOCKER or NONE; \
`recommendation_action` names the action for AUTONOMOUS/NEEDS_APPROVAL. Returns what is \
permitted next."""
        case_id, payment_id = _ids(state)
        snap = _snapshot(db, payment_id)
        refusal = _not_actionable(snap)
        if refusal:
            return f"Not recorded: {refusal}"
        risk = snap["risk"]
        blocker_up, kind_up = (blocker or "").upper(), (recommendation_kind or "").upper()
        action_up = (recommendation_action or "").upper() or None
        confidence_up = (confidence or "").upper()
        if blocker_up != risk["blockerType"]:
            # The label keys every gate downstream; it must be the computed one (defect R4).
            return (f"blocker {blocker!r} does not match the computed blocker "
                    f"{risk['blockerType']} (status {snap['status']}). Not recorded.")
        if confidence_up not in _CONFIDENCE_VALUES:
            return f"confidence must be one of {sorted(_CONFIDENCE_VALUES)}. Not recorded."
        if kind_up not in _KINDS:
            return f"recommendation_kind must be one of {sorted(_KINDS)}. Not recorded."
        if risk["riskLevel"] == risk_mod.ON_TRACK and kind_up != KIND_NONE:
            return "The payment is ON_TRACK: recommendation_kind must be NONE. Not recorded."
        if kind_up == KIND_AUTONOMOUS and action_up not in rules.AUTO:
            return f"An AUTONOMOUS recommendation names one of {sorted(rules.AUTO)}. Not recorded."
        if kind_up == KIND_NEEDS_APPROVAL and action_up not in rules.GATED:
            return f"A NEEDS_APPROVAL recommendation names one of {sorted(rules.GATED)}. Not recorded."
        if kind_up in (KIND_NONE, KIND_RESOLVE_BLOCKER):
            action_up = None
        cases.record_risk(db, case_id, risk)
        cases.set_agent_fields(db, case_id, {
            "diagnosis": _clean_text(diagnosis),
            "blocker": blocker_up,
            "evidence": evidence or [],
            "confidence": confidence_up,
            "reasoning": _clean_text(reasoning),
            "recommendation": {"action": action_up, "kind": kind_up,
                               "rationale": _clean_text(rationale)},
            "assessedAt": _now().isoformat(),
        }, push={"actionsTaken": _action_record(rules.RECORD_ASSESSMENT, "DONE", kind_up)})
        gated = _permitted_gated(db, case_id, payment_id)
        return (f"Recorded (risk {risk['riskLevel']}, phase {risk['phase']}). "
                f"Gated actions permitted: {gated or 'none'}.")

    @tool
    def propose_resolution(state: Annotated[dict, InjectedState], action: str,
                           rationale: str) -> str:
        """Propose HOLD_NEXT_VALUE_DATE, EXPEDITE or DEFER_NEXT_BUSINESS_DAY for human \
approval. Needs a recorded assessment; refused unless the policy permits it now."""
        case_id, payment_id = _ids(state)
        action_up = (action or "").upper()
        if not (cases.get(db, case_id).get("agent") or {}).get("assessedAt"):
            return "Refused: record_assessment first."
        refusal, snap = _gated_refusal(db, case_id, payment_id, action_up)
        if refusal:
            return f"Refused: {refusal}"
        risk = snap["risk"]
        cases.set_agent_fields(db, case_id, {"proposedAction": {
            "action": action_up, "rationale": _clean_text(rationale), "at": _now().isoformat(),
            "businessAt": snap["at"], "phase": risk["phase"], "riskLevel": risk["riskLevel"],
            "route": rules.ROUTE_FOR[action_up]["path"]}})
        cases.set_fields(db, case_id, {"status": cases.AWAITING_APPROVAL})
        return f"Proposed {action_up}; awaiting human approval."

    return [get_payment_context, get_cutoff_window, get_stage_timing, get_approval_status,
            get_screening_queue, get_funds_position, get_open_exceptions,
            send_approval_reminder, escalate_to_backup_approver, raise_screening_priority,
            notify_customer_funds, record_assessment, propose_resolution]


def execute_proposal(payment_id: str, case_id: str, proposal: dict, decided_by: str) -> dict:
    """Call the transactions route for an approved proposal. Raises ServiceRefused."""
    action = proposal["action"]
    if action == rules.HOLD_NEXT_VALUE_DATE:
        reason = f"Cutoff Agent case {case_id}: {proposal.get('rationale') or action}"
        return transactions_client.hold_next_value_date(payment_id, decided_by=decided_by,
                                                        reason=reason[:500])
    decision = rules.ROUTE_FOR[action]["body"]["decision"]
    return transactions_client.cutoff_decision(payment_id, decision=decision,
                                               decided_by=decided_by)


def _submitted_at(payment: dict) -> Optional[datetime]:
    for e in reversed((payment.get("lifecycle") or {}).get("events") or []):
        if e.get("state") == "SUBMITTED":
            return e.get("at")
    return None


def _as_utc(at: Any) -> Any:
    return at.replace(tzinfo=timezone.utc) if isinstance(at, datetime) and at.tzinfo is None else at


def verify_outcome(payment: dict, action: str, risk: Optional[dict]) -> Optional[str]:
    """The case outcome when the payment shows the approved decision, else None."""
    cutoff = payment.get("cutoff") or {}
    if action == rules.HOLD_NEXT_VALUE_DATE:
        return cases.HELD_NEXT_VALUE_DATE if cutoff.get("decision") == "HOLD_NEXT_VALUE_DATE" else None
    if action == rules.DEFER_NEXT_BUSINESS_DAY:
        # Any status but CUTOFF_EXCEPTION: release may have moved the payment past ROUTED.
        ok = payment.get("status") != "CUTOFF_EXCEPTION" and cutoff.get("decision") == "NEXT_VALUE_DATE"
        return cases.DEFERRED_NEXT_BUSINESS_DAY if ok else None
    if action == rules.EXPEDITE:
        if payment.get("status") not in _SUBMITTED_OR_LATER:
            return None
        submitted, external = _as_utc(_submitted_at(payment)), _as_utc((risk or {}).get("externalCutoffAt"))
        if submitted is not None and external is not None and submitted > external:
            return None
        return cases.SUBMITTED_IN_TIME
    return None


class CutoffState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    case_id: str
    payment_id: str
    decision: Optional[dict]
    verification: Optional[str]
    execute_error: Optional[str]
    reinvestigated: bool


def build_cutoff_agent(model: Any, db: Any, checkpointer: Any):
    """investigate ⇄ tools → (proposal? approval : END); approval → execute → verify.

    Same shape as the Reconciliation Agent: a raw `StateGraph` so the `interrupt()` gate and
    the post-approval execute/verify path have a place.
    """
    tools = _build_tools(db)
    model_bound = model.bind_tools(tools)
    tool_node = ToolNode(tools)

    def investigate(state: CutoffState) -> dict:
        messages = [SystemMessage(CUTOFF_SYSTEM_PROMPT), *state["messages"]]
        return {"messages": [model_bound.invoke(messages)]}

    def after_investigate(state: CutoffState) -> str:
        if getattr(state["messages"][-1], "tool_calls", None):
            return "tools"
        agent_doc = cases.get(db, state["case_id"]).get("agent") or {}
        return "approval" if agent_doc.get("proposedAction") else END

    def approval(state: CutoffState) -> dict:
        case_id = state["case_id"]
        case = cases.get(db, case_id)
        agent_doc = case.get("agent") or {}
        decision = interrupt({
            "caseId": case_id, "paymentId": state["payment_id"], "risk": _risk_view(case.get("risk")),
            "diagnosis": agent_doc.get("diagnosis"), "evidence": agent_doc.get("evidence"),
            "proposedAction": agent_doc.get("proposedAction"),
        })
        decision = decision if isinstance(decision, dict) else {"decision": APPROVE if decision else REJECT}
        verdict = (decision.get("decision") or REJECT).upper()
        record = {"decision": verdict, "by": decision.get("by") or "operator",
                  "note": decision.get("note"), "at": _now().isoformat()}
        fields: dict = {"approval": record}
        push = None
        if verdict != APPROVE:
            proposal = agent_doc.get("proposedAction") or {}
            fields["proposedAction"] = None
            if verdict == REJECT:
                # Not re-proposable in the same phase (approved Q11, `check_proposal`).
                push = {"rejectedActions": {"action": proposal.get("action"),
                                            "phase": proposal.get("phase"),
                                            "by": record["by"], "note": record["note"],
                                            "at": record["at"]}}
                cases.set_fields(db, case_id, {"status": cases.OPEN}, only_active=True)
        cases.set_agent_fields(db, case_id, fields, push=push)
        return {"decision": record}

    def after_approval(state: CutoffState) -> str:
        return "execute" if (state.get("decision") or {}).get("decision") == APPROVE else END

    def execute(state: CutoffState) -> dict:
        case_id, payment_id = state["case_id"], state["payment_id"]
        proposal = (cases.get(db, case_id).get("agent") or {}).get("proposedAction") or {}
        action = proposal.get("action")
        # Re-check at the execute boundary: the clock or the payment may have moved while paused.
        refusal, snap = _gated_refusal(db, case_id, payment_id, action)
        if refusal:
            cases.set_agent_fields(db, case_id, {}, push={
                "actionsTaken": _action_record(action, VERIFIED_REFUSED, refusal)})
            return {"verification": VERIFIED_REFUSED}
        try:
            execute_proposal(payment_id, case_id, proposal,
                             (state.get("decision") or {}).get("by") or "operator")
        except clients.ServiceRefused as e:
            cases.set_agent_fields(db, case_id, {}, push={
                "actionsTaken": _action_record(action, VERIFIED_REFUSED, e.detail)})
            return {"verification": VERIFIED_REFUSED}
        except Exception as e:  # noqa: BLE001 — 5xx/timeout: the write may have landed anyway.
            logger.warning("cutoff execute %s failed for %s", action, case_id, exc_info=True)
            error = f"{type(e).__name__}: {e}"[:300]
            cases.set_agent_fields(db, case_id, {}, push={
                "actionsTaken": _action_record(action, "ERROR", error)})
            # verify reads the payment and decides; the pause is spent either way.
            return {"verification": None, "execute_error": error}
        cases.record_risk(db, case_id, snap["risk"])
        cases.set_agent_fields(db, case_id, {}, push={
            "actionsTaken": _action_record(action, "DONE")})
        return {"verification": None}

    def verify(state: CutoffState) -> dict:
        case_id, payment_id = state["case_id"], state["payment_id"]
        case = cases.get(db, case_id)
        action = ((case.get("agent") or {}).get("proposedAction") or {}).get("action")
        result = state.get("verification")
        outcome = None
        if result is None:
            payment = db["payments"].find_one({"paymentId": payment_id}, {"_id": 0}) or {}
            outcome = verify_outcome(payment, action, case.get("risk"))
            result = VERIFIED if outcome else VERIFIED_NOT_APPLIED
        cases.set_agent_fields(db, case_id, {
            "verification": {"result": result, "outcome": outcome, "at": _now().isoformat(),
                             "executeError": state.get("execute_error")},
            "proposedAction": None})
        if outcome:
            payment = db["payments"].find_one({"paymentId": payment_id}, {"_id": 0}) or {}
            cases.close(db, case_id, result=outcome, payment_status=payment.get("status"),
                        value_date=(payment.get("cutoff") or {}).get("valueDate"),
                        at=cutoff_clock.now(db, case.get("clockRunId")))
        else:
            cases.set_fields(db, case_id, {"status": cases.OPEN}, only_active=True)
        return {"verification": result}

    def after_verify(state: CutoffState) -> str:
        if state.get("verification") == VERIFIED:
            return END
        return END if state.get("reinvestigated") else "reinvestigate"

    def reinvestigate(state: CutoffState) -> dict:
        return {"reinvestigated": True, "decision": None, "execute_error": None, "messages": [HumanMessage(
            f"The approved action did not take effect (verification: "
            f"{state.get('verification')}). Re-read the cut-off window and re-investigate "
            "once; propose only an action the policy now permits.")]}

    builder = StateGraph(CutoffState)
    builder.add_node("investigate", investigate)
    builder.add_node("tools", tool_node)
    builder.add_node("approval", approval)
    builder.add_node("execute", execute)
    builder.add_node("verify", verify)
    builder.add_node("reinvestigate", reinvestigate)
    builder.add_edge(START, "investigate")
    builder.add_conditional_edges("investigate", after_investigate,
                                  {"tools": "tools", "approval": "approval", END: END})
    builder.add_edge("tools", "investigate")
    builder.add_conditional_edges("approval", after_approval, {"execute": "execute", END: END})
    builder.add_edge("execute", "verify")
    builder.add_conditional_edges("verify", after_verify,
                                  {"reinvestigate": "reinvestigate", END: END})
    builder.add_edge("reinvestigate", "investigate")
    return builder.compile(checkpointer=checkpointer)


def _config(case_id: str) -> dict:
    return {"configurable": {"thread_id": case_id}}


def investigate(agent: Any, db: Any, case_id: str, payment_id: str) -> Optional[dict]:
    """Run (or re-run) the agent for one active case; returns the recorded `agent{}`.

    Never raises; a failed run is recorded in `agent.error` (message, at, attempts). A thread
    paused for approval is left alone.
    """
    case: dict = {}
    try:
        case = cases.get(db, case_id)
        if is_awaiting_approval(agent, case_id):
            logger.info("cutoff agent: %s awaits approval — not re-investigating", case_id)
            return case.get("agent")
        if not case.get("active"):
            return case.get("agent")
        snap = _snapshot(db, payment_id)
        cases.record_risk(db, case_id, snap["risk"])
        cases.set_agent_fields(db, case_id, {"startedAt": _now().isoformat(),
                                             "proposedAction": None})
        risk = snap["risk"] or {}
        user_msg = (f"{payment_id} is {snap['status']} (case {case_id}). Computed: phase "
                    f"{risk.get('phase')}, risk {risk.get('riskLevel')}, blocker "
                    f"{risk.get('blockerType')}, {risk.get('minutesToExternal')} min to the "
                    "external cut-off. Investigate and act.")
        agent.invoke({"messages": [HumanMessage(user_msg)], "case_id": case_id,
                      "payment_id": payment_id, "reinvestigated": False, "decision": None,
                      "verification": None, "execute_error": None},
                     config=_config(case_id))
    except Exception as e:  # noqa: BLE001 — never propagate to the worker.
        logger.warning("cutoff agent invoke failed for %s — case unchanged.", case_id,
                       exc_info=True)
        attempts = ((case.get("agent") or {}).get("error") or {}).get("attempts", 0) + 1
        cases.set_agent_fields(db, case_id, {"error": {
            "message": f"{type(e).__name__}: {e}"[:300], "at": _now(), "attempts": attempts}})
        return None
    cases.set_agent_fields(db, case_id, {"error": None})
    return cases.get(db, case_id).get("agent")


def resume(agent: Any, db: Any, case_id: str, decision: str, note: Optional[str] = None,
           by: str = "operator") -> Optional[dict]:
    """Resume a paused thread with the human's decision; returns the recorded `agent{}`."""
    from langgraph.types import Command

    agent.invoke(Command(resume={"decision": decision, "note": note, "by": by}),
                 config=_config(case_id))
    return cases.get(db, case_id).get("agent")


def supersede(agent: Any, db: Any, case_id: str, *, result: str = cases.SUPERSEDED,
              note: Optional[str] = None) -> None:
    """Close a case the world has moved past (a new demo run, a payment released by hand).
    A paused proposal is resumed as SUPERSEDED first so the thread does not dangle."""
    if is_awaiting_approval(agent, case_id):
        resume(agent, db, case_id, SUPERSEDED, note=note, by=ACTOR)
    case = cases.get(db, case_id)
    payment = db["payments"].find_one({"paymentId": case.get("paymentId")}, {"_id": 0}) or {}
    # Business time, like verify; None (run gone → CLOCK_EXPIRED) falls back to real time.
    cases.close(db, case_id, result=result, payment_status=payment.get("status"),
                value_date=case.get("valueDate"), at=cutoff_clock.now(db, case.get("clockRunId")))
