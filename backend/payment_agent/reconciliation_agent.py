"""The Reconciliation Agent — investigates an OPEN reconciliation exception, classifies its
cause, and proposes the one action the code permits for that cause.

Woken by `reconciliation_worker` for the three categories the ledger raises at stage 8:
`RECONCILIATION_DISCREPANCY`, `RECONCILIATION_MISSING` and `ORPHANED_SETTLEMENT` (parent plan
§Part C). The deterministic engine decides whether records reconcile; the agent never does.
Its job is the reasoning after a break: gather the trace, the correspondent's habits, past
resolutions and candidate statement lines, then name a cause and an action.

## What the agent may do

- **On its own:** `recheck_reconciliation` (resolves only if the engine then says RECONCILED)
  and `schedule_recheck` (defers its own next look, capped). Timing lag is its to wait out.
- **With approval:** everything else. `propose_action` records a proposal; the graph pauses
  at `approval` (`interrupt()`); `POST /reconciliation/{id}/approve` resumes it with the
  operator's decision. On APPROVE, `execute` calls the owning service's route and `verify`
  closes the case only when the exception is no longer OPEN (or, for an escalation, is
  awaiting the correspondent). A failed fix gets one re-investigation, then an escalation.

Which actions are permitted is decided in code (`policy.py`), checked at propose AND again at
execute, with the services' own guards behind both. The prompt describes the rules; it does
not enforce them (defect 2026-09-28 A2).

## Write surface

The agent writes only its own annotation, `exceptions.agent{}`, directly. Every change to an
exception's status, and every money movement, goes through the transactions/ledger routes
(`clients.py`). Tools read the exception and payment ids from graph state, never from the
model's arguments, so a run can act only on the exception it was woken for. `MongoDBSaver`
persists graph state keyed by `thread_id = exceptionId`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import InjectedState, ToolNode
from langgraph.types import interrupt
from typing_extensions import Annotated

import clients
import policy
import recon_evidence
# `payment_trace`, not `trace` — the stdlib owns that name, and this service directory leads
# sys.path, so a module called `trace.py` here shadows it for the whole process.
import payment_trace as trace_mod

logger = logging.getLogger(__name__)

# A label, not a numeric score — an LLM's numeric confidence is not calibrated.
_CONFIDENCE_VALUES = {"HIGH", "MEDIUM", "LOW"}

# Some model responses leak their own tool-call markup (e.g. a stray closing
# tag like `</rationale>` or `</invoke>`) onto the end of a free-text field.
# Strip it before persisting so `agent{}` never surfaces a malformed fragment.
_TRAILING_TAG_LEAK = re.compile(r"(\s*</[a-zA-Z_][\w:-]*>\s*)+$")


def _clean_text(value: str) -> str:
    return _TRAILING_TAG_LEAK.sub("", value or "").rstrip()

APPROVE = "APPROVE"
REJECT = "REJECT"

VERIFIED_RESOLVED = "RESOLVED"
VERIFIED_ESCALATED = "ESCALATED"
VERIFIED_OPEN = "STILL_OPEN"
VERIFIED_REFUSED = "REFUSED"

RECONCILIATION_SYSTEM_PROMPT = """\
You are the Reconciliation Agent for Leafy Bank. The deterministic reconciliation engine \
opened an exception at stage 8; it has already decided the records do not (yet) reconcile. \
Find out WHY, and resolve it with the one action the bank's policy permits.

Exception categories:
- RECONCILIATION_DISCREPANCY: a statement line was matched but an amount differs.
- RECONCILIATION_MISSING: a wire settled but no correspondent statement line arrived in time.
- ORPHANED_SETTLEMENT: a statement line arrived that matches no payment.

Steps:
1. `payment_trace_lookup` and `reconciliation_analysis` (skip both for an ORPHANED line — it \
has no payment; the exception detail describes the line).
2. `get_correspondent_profile` (charge policy, reference format, usual booking lag) and \
`get_resolution_precedents` (how similar cases were resolved before).
3. For MISSING or ORPHANED: `find_statement_candidates`.
4. `record_investigation` with a cause: TIMING_LAG, REFERENCE_MISMATCH, AMOUNT_MISMATCH, FEE \
or ORPHANED. Cite specific records and amounts as evidence. The reply lists the actions \
permitted for that cause.
5. Act:
   - TIMING_LAG: `recheck_reconciliation`; if still open, `schedule_recheck` within the \
correspondent's usual lag. Do not escalate a timing lag until rechecks are exhausted.
   - Otherwise: `propose_action` with one permitted action. An operator approves it.

Rules:
- Only permitted actions are accepted; a refused proposal explains why — fix it or pick \
another permitted action. Never invent a statement line: LINK uses a candidate index.
- A FEE cause is accepted only when the discrepancy equals a charge the correspondent's \
chargePolicy actually levies (else the bank default 25.0). Any other delta is an \
AMOUNT_MISMATCH: escalate it, never accept it.
- For FEE, the payment's chargeBearer decides the books, not you.
- An adjustment amount must equal the discrepancy exactly.
- End with a two-sentence summary for the operator.
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _exception(db: Any, exception_id: str) -> dict:
    return db["exceptions"].find_one({"exceptionId": exception_id}, {"_id": 0}) or {}


def _is_statement_line(exc: dict) -> bool:
    return (exc.get("subjectRef") or {}).get("kind") == "STATEMENT_LINE"


def _charge_bearer(db: Any, exc: dict) -> Optional[str]:
    if _is_statement_line(exc):
        return None
    payment = db["payments"].find_one({"paymentId": exc.get("paymentId")},
                                      {"_id": 0, "chargeBearer": 1}) or {}
    return payment.get("chargeBearer")


def _known_charges(db: Any, exc: dict) -> list:
    """Every charge this exception's correspondent levies (recon_evidence)."""
    return recon_evidence.known_charges(db, _correspondent_bic(db, exc, exc.get("paymentId")))


def set_agent_fields(db: Any, exception_id: str, fields: dict, push: Optional[dict] = None) -> None:
    """Merge `fields` into `exceptions.agent{}`. A dotted `$set` fails on a null parent, so
    an exception the agent has not touched yet gets an empty `agent{}` first."""
    coll = db["exceptions"]
    coll.update_one({"exceptionId": exception_id, "agent": None}, {"$set": {"agent": {}}})
    update: dict = {"$set": {f"agent.{k}": v for k, v in fields.items()}}
    if push:
        update["$push"] = {f"agent.{k}": v for k, v in push.items()}
    coll.update_one({"exceptionId": exception_id}, update)


def _action_record(action: str, result: str, detail: Any = None) -> dict:
    return {"action": action, "at": _now().isoformat(), "result": result, "detail": detail}


def _ids(state: dict) -> tuple[str, str]:
    return state["exception_id"], state["payment_id"]


def _build_tools(db: Any):
    """The agent's tools. Ids come from graph state (`InjectedState`), not from the model."""

    @tool
    def payment_trace_lookup(state: Annotated[dict, InjectedState]) -> str:
        """Every record of this payment across the lifecycle: payment, executions, \
transactions, ledger events, settlement positions, reconciliation items, the exception."""
        exception_id, payment_id = _ids(state)
        return json.dumps(trace_mod.gather_trace(db, payment_id, exception_id), default=str)

    @tool
    def reconciliation_analysis(state: Annotated[dict, InjectedState]) -> str:
        """Per-leg reconciliation breakdown in major units: each leg's result, left and right \
amounts, the delta (left - right) and the reason a leg is pending."""
        exception_id, payment_id = _ids(state)
        trace = trace_mod.gather_trace(db, payment_id, exception_id)
        legs = [{
            "leg": leg.get("leg"),
            "result": leg.get("result"),
            "left": _major(leg.get("leftAmount")),
            "right": _major(leg.get("rightAmount")),
            "delta": _major(_delta(leg.get("leftAmount"), leg.get("rightAmount"))),
            "reason": leg.get("reason"),
            "detail": leg.get("detail"),
        } for item in trace.get("reconciliationItems") or [] for leg in item.get("legs") or []]
        exc = trace.get("exception") or {}
        return json.dumps({"legs": legs, "exceptionDetail": exc.get("detail"),
                           "chargeBearer": (trace.get("payment") or {}).get("chargeBearer")},
                          default=str)

    @tool
    def find_statement_candidates(state: Annotated[dict, InjectedState]) -> str:
        """For a MISSING payment: unmatched statement lines that could be it. For an ORPHANED \
line: unreconciled payments it could belong to. Scored deterministically by reference \
(exact / re-keyed prefix / shared token) and amount. Use the returned index with LINK."""
        exception_id, _ = _ids(state)
        candidates = recon_evidence.find_candidates(db, _exception(db, exception_id))
        set_agent_fields(db, exception_id, {"candidates": candidates})
        return json.dumps([{"index": i, **c} for i, c in enumerate(candidates)] or
                          "No candidates found.", default=str)

    @tool
    def get_correspondent_profile(state: Annotated[dict, InjectedState]) -> str:
        """The correspondent bank's charge policy, reference format and usual booking lag \
(p50/p90 seconds from submission to statement booking)."""
        exception_id, payment_id = _ids(state)
        bic = _correspondent_bic(db, _exception(db, exception_id), payment_id)
        if not bic:
            return "No correspondent on this payment (a domestic or internal settlement)."
        return json.dumps(recon_evidence.correspondent_profile(db, bic), default=str)

    @tool
    def get_resolution_precedents(state: Annotated[dict, InjectedState]) -> str:
        """The last resolved exceptions of this category with this correspondent, and the \
action each was closed with."""
        exception_id, payment_id = _ids(state)
        exc = _exception(db, exception_id)
        bic = _correspondent_bic(db, exc, payment_id)
        if not bic:
            return "No correspondent to look up precedents for."
        return json.dumps(recon_evidence.resolution_precedents(db, bic, exc.get("category")),
                          default=str)

    @tool
    def record_investigation(
        state: Annotated[dict, InjectedState],
        cause: str,
        root_cause: str,
        evidence: list[str],
        confidence: str,
        investigation: str = "",
    ) -> str:
        """Record your findings. `cause` is one of TIMING_LAG, REFERENCE_MISMATCH, \
AMOUNT_MISMATCH, FEE, ORPHANED. `confidence` is HIGH, MEDIUM or LOW. `evidence` is a list of \
short strings each citing a specific record and amount. Returns the permitted actions."""
        exception_id, _ = _ids(state)
        confidence_up = (confidence or "").upper()
        cause_up = (cause or "").upper()
        if confidence_up not in _CONFIDENCE_VALUES:
            return f"confidence must be one of {sorted(_CONFIDENCE_VALUES)}; got {confidence!r}. Not recorded."
        if cause_up not in policy.CAUSES:
            return f"cause must be one of {list(policy.CAUSES)}; got {cause!r}. Not recorded."
        exc = _exception(db, exception_id)
        if not exc:
            return f"No exception found with exceptionId {exception_id}."
        if cause_up == policy.FEE and exc.get("category") == policy.CATEGORY_DISCREPANCY:
            # A fee the correspondent does not levy is a rationalised amount mismatch
            # (defect 2026-10-03, R4) — refuse the label before anything keys on it.
            refusal = policy.fee_cause_refusal(
                (exc.get("detail") or {}).get("discrepancyAmount"), _known_charges(db, exc))
            if refusal:
                return f"FEE refused: {refusal}"
        allowed = sorted(policy.allowed_actions(exc.get("category"), cause_up, _charge_bearer(db, exc)))
        set_agent_fields(db, exception_id, {
            "cause": cause_up,
            "investigation": _clean_text(investigation),
            "rootCause": _clean_text(root_cause),
            "evidence": evidence or [],
            "confidence": confidence_up,
            "recommendedResolution": None,
            "recordedAt": _now().isoformat(),
        })
        if not allowed:
            return (f"Recorded (cause={cause_up}). No action is permitted for a "
                    f"{exc.get('category')} with that cause — re-check the cause.")
        return f"Recorded (cause={cause_up}, confidence={confidence_up}). Permitted actions: {allowed}."

    @tool
    def recheck_reconciliation(state: Annotated[dict, InjectedState]) -> str:
        """Re-match statements and re-run this payment's tie-out. Resolves the exception only \
if the engine now says RECONCILED. You may call this without approval."""
        exception_id, _ = _ids(state)
        refusal = _refusal(db, exception_id, policy.RECHECK, {})
        if refusal:
            return refusal
        agent_doc = _exception(db, exception_id).get("agent") or {}
        count = int(agent_doc.get("recheckCount") or 0)
        if count >= policy.MAX_RECHECKS:
            return (f"Rechecks exhausted ({count}). Propose ESCALATE_TO_CORRESPONDENT.")
        try:
            out = clients.recheck(exception_id, note="Reconciliation Agent re-check")
        except clients.ServiceRefused as e:
            set_agent_fields(db, exception_id, {}, push={
                "actionsTaken": _action_record(policy.RECHECK, VERIFIED_REFUSED, e.detail)})
            return f"Recheck refused: {e.detail}"
        status = (out.get("exception") or {}).get("status")
        set_agent_fields(db, exception_id, {"recheckCount": count + 1}, push={
            "actionsTaken": _action_record(policy.RECHECK, out.get("outcome") or "UNKNOWN")})
        if status != "OPEN":
            set_agent_fields(db, exception_id, {"nextCheckAt": None, "verification": {
                "result": VERIFIED_RESOLVED, "at": _now().isoformat()}})
            return f"Recheck reconciled the payment (outcome {out.get('outcome')}). Case closed."
        return f"Still {out.get('outcome')} after recheck {count + 1}/{policy.MAX_RECHECKS}."

    @tool
    def schedule_recheck(state: Annotated[dict, InjectedState], minutes: int) -> str:
        """Look at this exception again in `minutes` (capped). Use after a recheck finds the \
statement line still missing and the correspondent's lag says it may yet arrive."""
        exception_id, _ = _ids(state)
        refusal = _refusal(db, exception_id, policy.RECHECK, {})
        if refusal:
            return refusal
        m = policy.capped_recheck_minutes(minutes)
        at = _now() + timedelta(minutes=m)
        set_agent_fields(db, exception_id, {"nextCheckAt": at})
        return f"Next check scheduled in {m} minute(s), at {at.isoformat()}."

    @tool
    def propose_action(
        state: Annotated[dict, InjectedState],
        action: str,
        rationale: str,
        amount: Optional[float] = None,
        candidate_index: Optional[int] = None,
    ) -> str:
        """Propose one resolution for operator approval: LINK_STATEMENT_ENTRY (needs \
candidate_index), POST_ADJUSTMENT (needs amount = the discrepancy), ACCEPT_DISCREPANCY, \
ESCALATE_TO_CORRESPONDENT, DISMISS or RECHECK. Refused unless permitted for the cause."""
        exception_id, _ = _ids(state)
        action_up = (action or "").upper()
        params = {"amount": amount, "candidateIndex": candidate_index}
        refusal = _refusal(db, exception_id, action_up, params)
        if refusal:
            return f"Refused: {refusal}"
        if action_up == policy.LINK:
            candidate = (_exception(db, exception_id).get("agent") or {})["candidates"][candidate_index]
            params["target"] = {k: candidate.get(k) for k in
                                ("paymentId", "paymentMessageId", "lineNo", "reference", "amount", "score")}
        set_agent_fields(db, exception_id, {
            "proposedAction": {"action": action_up, "params": params,
                               "rationale": _clean_text(rationale),
                               "at": _now().isoformat()},
            "recommendedResolution": action_up,
            "nextCheckAt": None,
        })
        return f"Proposed {action_up}; awaiting operator approval."

    return [payment_trace_lookup, reconciliation_analysis, find_statement_candidates,
            get_correspondent_profile, get_resolution_precedents, record_investigation,
            recheck_reconciliation, schedule_recheck, propose_action]


def _major(minor) -> Optional[float]:
    return None if minor is None else round(minor / 100.0, 2)


def _delta(left, right) -> Optional[int]:
    return None if left is None or right is None else left - right


def _correspondent_bic(db: Any, exc: dict, payment_id: str) -> Optional[str]:
    if _is_statement_line(exc):
        subject = exc.get("subjectRef") or {}
        stmt = db["paymentMessages"].find_one({"paymentMessageId": subject.get("paymentMessageId")},
                                              {"_id": 0, "entries": 1}) or {}
        line = next((e for e in stmt.get("entries") or [] if e.get("lineNo") == subject.get("lineNo")), {})
        return line.get("counterpartyBic")
    payment = db["payments"].find_one({"paymentId": payment_id}, {"_id": 0, "correspondent": 1}) or {}
    return (payment.get("correspondent") or {}).get("correspondentBic")


def _refusal(db: Any, exception_id: str, action: str, params: dict) -> Optional[str]:
    """The policy check, against the exception as it stands in the DB right now."""
    exc = _exception(db, exception_id)
    if exc.get("status") != "OPEN":
        return f"Exception {exception_id} is {exc.get('status')}, not OPEN — nothing to do."
    agent_doc = exc.get("agent") or {}
    known = (_known_charges(db, exc)
             if exc.get("category") == policy.CATEGORY_DISCREPANCY
             and agent_doc.get("cause") == policy.FEE else None)
    return policy.check_proposal(
        category=exc.get("category"), cause=agent_doc.get("cause"),
        charge_bearer=_charge_bearer(db, exc), action=action, params=params,
        candidates=agent_doc.get("candidates") or [],
        discrepancy_amount=(exc.get("detail") or {}).get("discrepancyAmount"),
        known_charges=known,
    )


def execute_proposal(db: Any, exc: dict, proposal: dict) -> dict:
    """Call the owning service's route for an approved proposal. Raises ServiceRefused."""
    exception_id = exc["exceptionId"]
    action = proposal["action"]
    note = f"Approved agent proposal: {proposal.get('rationale') or action}"
    if action == policy.RECHECK:
        return clients.recheck(exception_id, note=note)
    if action == policy.LINK:
        target = (proposal.get("params") or {}).get("target") or {}
        if _is_statement_line(exc):
            return clients.link(exception_id, payment_id=target.get("paymentId"), note=note)
        return clients.link(exception_id, payment_message_id=target.get("paymentMessageId"),
                            line_no=target.get("lineNo"), note=note)
    return clients.resolve(exception_id, action, note=note)


class ReconciliationState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    exception_id: str
    payment_id: str
    decision: Optional[dict]
    verification: Optional[str]
    reinvestigated: bool


def build_reconciliation_agent(model: Any, db: Any, checkpointer: Any):
    """investigate ⇄ tools → (proposal? approval : END); approval → execute → verify.

    A raw `StateGraph`, not `create_agent`: the flow needs an `interrupt()` gate and a
    post-approval execute/verify path that a tool-calling loop has no place for.
    """
    tools = _build_tools(db)
    model_bound = model.bind_tools(tools)
    tool_node = ToolNode(tools)

    def investigate(state: ReconciliationState) -> dict:
        messages = [SystemMessage(RECONCILIATION_SYSTEM_PROMPT), *state["messages"]]
        return {"messages": [model_bound.invoke(messages)]}

    def after_investigate(state: ReconciliationState) -> str:
        if getattr(state["messages"][-1], "tool_calls", None):
            return "tools"
        agent_doc = _exception(db, state["exception_id"]).get("agent") or {}
        return "approval" if agent_doc.get("proposedAction") else END

    def approval(state: ReconciliationState) -> dict:
        agent_doc = _exception(db, state["exception_id"]).get("agent") or {}
        decision = interrupt({
            "exceptionId": state["exception_id"],
            "cause": agent_doc.get("cause"),
            "confidence": agent_doc.get("confidence"),
            "evidence": agent_doc.get("evidence"),
            "proposedAction": agent_doc.get("proposedAction"),
        })
        decision = decision if isinstance(decision, dict) else {"decision": APPROVE if decision else REJECT}
        record = {"decision": (decision.get("decision") or REJECT).upper(),
                  "by": decision.get("by") or "operator", "note": decision.get("note"),
                  "at": _now().isoformat()}
        set_agent_fields(db, state["exception_id"], {"approval": record})
        return {"decision": record}

    def after_approval(state: ReconciliationState) -> str:
        return "execute" if (state.get("decision") or {}).get("decision") == APPROVE else END

    def execute(state: ReconciliationState) -> dict:
        exception_id = state["exception_id"]
        exc = _exception(db, exception_id)
        proposal = (exc.get("agent") or {}).get("proposedAction") or {}
        action = proposal.get("action")
        # Re-check at the execute boundary: the exception may have moved while paused.
        refusal = _refusal(db, exception_id, action, proposal.get("params") or {})
        if refusal:
            set_agent_fields(db, exception_id, {}, push={
                "actionsTaken": _action_record(action, VERIFIED_REFUSED, refusal)})
            return {"verification": VERIFIED_REFUSED}
        try:
            out = execute_proposal(db, exc, proposal)
        except clients.ServiceRefused as e:
            set_agent_fields(db, exception_id, {}, push={
                "actionsTaken": _action_record(action, VERIFIED_REFUSED, e.detail)})
            return {"verification": VERIFIED_REFUSED}
        set_agent_fields(db, exception_id, {}, push={
            "actionsTaken": _action_record(action, "DONE", out.get("outcome"))})
        return {"verification": None}

    def verify(state: ReconciliationState) -> dict:
        exception_id = state["exception_id"]
        result = state.get("verification")
        if result is None:
            exc = _exception(db, exception_id)
            if exc.get("status") == "OPEN" and not _is_statement_line(exc):
                try:  # the deterministic engine has the last word
                    clients.reconcile(state["payment_id"])
                except clients.ServiceRefused:
                    pass  # 409: no longer eligible, i.e. already reconciled
                exc = _exception(db, exception_id)
            if exc.get("status") != "OPEN":
                result = VERIFIED_RESOLVED
            elif exc.get("awaitingCounterparty"):
                result = VERIFIED_ESCALATED
            else:
                result = VERIFIED_OPEN
        set_agent_fields(db, exception_id, {
            "verification": {"result": result, "at": _now().isoformat()},
            "proposedAction": None})
        return {"verification": result}

    def after_verify(state: ReconciliationState) -> str:
        if state.get("verification") in (VERIFIED_RESOLVED, VERIFIED_ESCALATED):
            return END
        return END if state.get("reinvestigated") else "reinvestigate"

    def reinvestigate(state: ReconciliationState) -> dict:
        return {"reinvestigated": True, "decision": None, "messages": [HumanMessage(
            f"The approved action did not close the exception (verification: "
            f"{state.get('verification')}). Re-investigate once. If no permitted action can "
            "close it, propose ESCALATE_TO_CORRESPONDENT.")]}

    builder = StateGraph(ReconciliationState)
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


def _config(exception_id: str) -> dict:
    return {"configurable": {"thread_id": exception_id}}


# Graph-state keys a tool may receive via `InjectedState`; never part of what the model chose.
_INJECTED_ARG_KEYS = {"state"}
_TOOL_RESULT_MAX = 600
_NOTE_MAX = 300


def _text_of(content: Any) -> str:
    """A message's text: a plain string, or the text blocks of a content list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b if isinstance(b, str) else b.get("text", "")
                         for b in content
                         if isinstance(b, str) or (isinstance(b, dict) and b.get("type") == "text"))
    return ""


def _step(kind: str, text: Optional[str] = None, tool: Optional[str] = None,
          args: Optional[dict] = None) -> dict:
    return {"kind": kind, "text": text, "tool": tool, "args": args}


def messages_to_steps(messages: list) -> list[dict]:
    """Shape a thread's messages into the read-only thinking timeline (plan E)."""
    steps = []
    for msg in messages:
        kind = getattr(msg, "type", None)
        if kind == "ai":
            text = _clean_text(_text_of(msg.content))
            if text:
                steps.append(_step("thought", text=text))
            for call in getattr(msg, "tool_calls", None) or []:
                args = {k: v for k, v in (call.get("args") or {}).items() if k not in _INJECTED_ARG_KEYS}
                steps.append(_step("tool_call", tool=call.get("name"), args=args))
        elif kind == "tool":
            steps.append(_step("tool_result", text=_text_of(msg.content)[:_TOOL_RESULT_MAX],
                               tool=getattr(msg, "name", None)))
        elif kind == "human":
            steps.append(_step("note", text=_text_of(msg.content)[:_NOTE_MAX]))
    return steps


def thread_messages(agent: Any, exception_id: str) -> list:
    """The checkpointed messages of an exception's thread; empty when there is no thread."""
    state = agent.get_state(_config(exception_id))
    return list((state.values or {}).get("messages", []))


def is_awaiting_approval(agent: Any, exception_id: str) -> bool:
    """True only when the thread is paused at the approval `interrupt()`.

    `state.next` alone is wrong: a run that crashed (expired SSO token) also leaves a pending
    node, which made a failed thread look like it was waiting for the operator — so it was
    never retried."""
    try:
        return any(task.interrupts for task in agent.get_state(_config(exception_id)).tasks)
    except Exception:  # noqa: BLE001
        return False


# A failed run (expired AWS SSO token, Bedrock throttling) is retried by the worker's sweep
# this long after the failure, up to MAX_ERROR_ATTEMPTS times; the manual trigger always works.
ERROR_RETRY_AFTER_SECONDS = 60
MAX_ERROR_ATTEMPTS = 5


def investigate(agent: Any, db: Any, exception_id: str, payment_id: str) -> Optional[dict]:
    """Run (or re-run) the agent for one OPEN exception; returns the recorded `agent{}`.

    Never raises; a failed run is recorded in `agent.error` (message, at, attempts) so the
    UI can show it and the sweep can retry. A thread paused for approval is left alone — re-investigating would
    abandon the proposal the operator is looking at.
    """
    if is_awaiting_approval(agent, exception_id):
        logger.info("reconciliation agent: %s awaits approval — not re-investigating", exception_id)
        return _exception(db, exception_id).get("agent")
    exc = _exception(db, exception_id)
    if exc.get("status") != "OPEN":
        return exc.get("agent")
    set_agent_fields(db, exception_id, {"startedAt": _now().isoformat(), "nextCheckAt": None,
                                        "proposedAction": None})
    user_msg = (f"{exc.get('category')} on {payment_id} (exception {exception_id}). "
                f"Detail: {json.dumps(exc.get('detail'), default=str)}. Investigate and act.")
    try:
        agent.invoke({"messages": [HumanMessage(user_msg)], "exception_id": exception_id,
                      "payment_id": payment_id, "reinvestigated": False},
                     config=_config(exception_id))
    except Exception as e:  # noqa: BLE001 — never propagate to the worker.
        logger.warning("reconciliation agent invoke failed for %s — exception unchanged.",
                       exception_id, exc_info=True)
        attempts = ((exc.get("agent") or {}).get("error") or {}).get("attempts", 0) + 1
        set_agent_fields(db, exception_id, {"error": {
            "message": f"{type(e).__name__}: {e}"[:300], "at": _now(), "attempts": attempts}})
        return None
    set_agent_fields(db, exception_id, {"error": None})
    return _exception(db, exception_id).get("agent")


def resume(agent: Any, db: Any, exception_id: str, decision: str, note: Optional[str] = None,
           by: str = "operator") -> Optional[dict]:
    """Resume a paused thread with the operator's decision; returns the recorded `agent{}`."""
    from langgraph.types import Command

    agent.invoke(Command(resume={"decision": decision, "note": note, "by": by}),
                 config=_config(exception_id))
    return _exception(db, exception_id).get("agent")
