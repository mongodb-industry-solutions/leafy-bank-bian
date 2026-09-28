"""The Phase-1 Reconciliation Agent — invoked async on a reconciliation mismatch.

Triggered by the ledger service's deterministic reconciliation engine when it stamps a payment
DISCREPANT and opens a `RECONCILIATION_DISCREPANCY` exception (reconciliation_service.
_stamp_discrepant). The deterministic engine decides whether the records mathematically
reconcile; this agent never makes that decision. Its job is the *reasoning after a mismatch
is detected* (spec L1274): gather the full payment lifecycle, identify the likely root cause
of the discrepancy (fee, settlement adjustment, delayed record, posting error), and record its
findings + a recommended resolution onto the exception's reserved `exceptions.agent{}` subdoc.

## Human-in-the-loop

The agent does NOT resolve the exception and does NOT modify ledger or journal records (spec
L1286). It records a `recommendedResolution`; a Payments Operations user reviews that
recommendation and approves the actual resolution through the existing transactions UI
(`POST /workflow/exceptions/{id}/resolve`) — the spec's own HITL model (L1323): *"the
Payments Operations user can review the recommendation and approve actions that require human
authorization."* An in-graph `interrupt()`-based approval gate is a future enhancement (the
plan's deferred supervisor / Payments Operations Agent), not Phase 1.

## Write surface

The one write the agent performs is `$set: {"agent": {...}}` on the exception document —
exactly the landing zone the schema reserves (Q61). It never touches the exception's `status`,
`category`, `resolution`, or any ledger/journal record. `MongoDBSaver` persists the agent's
investigation state in Atlas keyed by `thread_id = exceptionId`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional, TypedDict

from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import interrupt
from typing_extensions import Annotated

# `payment_trace`, not `trace` — the stdlib owns that name, and this service directory leads
# sys.path, so a module called `trace.py` here shadows it for the whole process (including
# any dependency that imports the stdlib one).
import payment_trace as trace_mod

logger = logging.getLogger(__name__)

# Confidence is a free-text label the agent picks from {HIGH, MEDIUM, LOW} — not a numeric
# score, because an LLM's numeric confidence is not calibrated. The operator sees the label.
_CONFIDENCE_VALUES = {"HIGH", "MEDIUM", "LOW"}

RECONCILIATION_SYSTEM_PROMPT = """\
You are the Reconciliation Agent for Leafy Bank, invoked at Stage 8 after the deterministic \
reconciliation engine flagged a mismatch on a payment.

The deterministic engine has ALREADY decided the records do not reconcile. Do not re-check \
whether they reconcile — assume the mismatch is real. Your job is to investigate WHY they \
disagree and recommend a resolution.

Steps:
1. Call `payment_trace_lookup` with the paymentId to gather every record across the lifecycle \
(payment, executions, transactions, ledger events, settlement positions, reconciliation items, \
the exception itself).
2. Call `reconciliation_analysis` with the paymentId to get the per-leg expected-vs-actual \
breakdown and the discrepancy amount.
3. Reason across the trace to identify the LIKELY root cause: a fee or charge deducted by a \
correspondent, a settlement adjustment / FX residual, a delayed record not yet posted, an \
incorrect posting, or another known condition. Cite the specific records and amounts that \
support your conclusion.
4. Call `record_investigation` with your findings — root cause, the evidence (list of \
specific record/amount citations), a confidence label (HIGH/MEDIUM/LOW), and a recommended \
resolution action.

Rules:
- You investigate and recommend ONLY. Do NOT resolve the exception, do NOT modify ledger or \
journal records, do NOT move money. The operator approves any resolution.
- `record_investigation` is your only write. Use it exactly once, after your analysis.
- If the trace is incomplete (records missing), say so in the investigation and recommend \
manual review with confidence LOW.

Respond with a short summary of your investigation once `record_investigation` has been called.
"""


def _build_tools(db: Any):
    """Build the agent's tools, closing over the DB handle."""

    @tool
    def payment_trace_lookup(payment_id: str) -> str:
        """Gather every record sharing this paymentId across the lifecycle collections: \
payment, paymentExecutions, transactions, ledgerEvents, settlementPositions, \
reconciliationItems, and the exception. Returns the trace as JSON."""
        return json.dumps(
            trace_mod.gather_trace(db, payment_id), default=str
        )

    @tool
    def reconciliation_analysis(payment_id: str) -> str:
        """Return the per-leg reconciliation breakdown for a payment: each leg's expected vs \
actual amount and the discrepancy. Draws from reconciliationItems and settlementPositions."""
        items = trace_mod.gather_trace(db, payment_id)
        legs = []
        # `reconciliationItems` is one doc whose `legs[]` array carries `leftAmount`/
        # `rightAmount`/`result`/`detail` PER LEG. Iterate the legs, not read off the item.
        for item in items.get("reconciliationItems") or []:
            for leg in item.get("legs") or []:
                legs.append({
                    "leg": leg.get("leg"),
                    "leftAmount": leg.get("leftAmount"),
                    "rightAmount": leg.get("rightAmount"),
                    "result": leg.get("result"),
                    "detail": leg.get("detail"),
                })
        positions = items.get("settlementPositions") or []
        return json.dumps({
            "reconciliationLegs": legs,
            "settlementPositions": [
                {"expected": p.get("expectedAmount", p.get("grossAmount")),
                 "actual": p.get("actualAmount"),
                 "outcome": p.get("outcome")}
                for p in positions
            ],
            "discrepancyHint": _discrepancy_hint(legs, positions),
        }, default=str)

    @tool
    def record_investigation(
        exception_id: str,
        root_cause: str,
        evidence: list[str],
        confidence: str,
        recommended_resolution: str,
        investigation: str = "",
    ) -> str:
        """Record the investigation findings onto the exception's reserved `agent{}` subdoc. \
This is the only write you may perform. `confidence` must be one of HIGH, MEDIUM, LOW. \
`evidence` is a list of short strings each citing a specific record and amount."""
        confidence_up = (confidence or "").upper()
        if confidence_up not in _CONFIDENCE_VALUES:
            return f"confidence must be one of {sorted(_CONFIDENCE_VALUES)}; got {confidence!r}. Not recorded."
        coll = db["exceptions"]
        try:
            result = coll.update_one(
                {"exceptionId": exception_id},
                {"$set": {
                    "agent": {
                        "investigation": investigation,
                        "rootCause": root_cause,
                        "evidence": evidence or [],
                        "confidence": confidence_up,
                        "recommendedResolution": recommended_resolution,
                        "recordedAt": datetime.now(timezone.utc).isoformat(),
                    }
                }},
            )
        except Exception:  # noqa: BLE001 — never propagate; the operator still has the raw exception.
            logger.warning(
                "record_investigation failed on %s — the exception is unchanged.",
                exception_id, exc_info=True,
            )
            return f"Failed to record investigation on {exception_id}."
        if result.matched_count == 0:
            return f"No exception found with exceptionId {exception_id}."
        return f"Recorded investigation on {exception_id} (rootCause={root_cause}, confidence={confidence_up})."

    return [payment_trace_lookup, reconciliation_analysis, record_investigation]


def _discrepancy_hint(legs: list[dict], positions: list[dict]) -> str:
    """A plain-language summary the agent can use as a starting hypothesis."""
    disc = next((l.get("discrepancyAmount") for l in legs if l.get("discrepancyAmount")), None)
    if disc:
        return f"Discrepancy of {disc} detected across reconciliation legs."
    expected = (positions[0].get("expected") if positions else None)
    actual = (positions[0].get("actual") if positions else None)
    if expected is not None and actual is not None and expected != actual:
        return f"Settlement expected {expected} vs actual {actual}."
    return "No numeric discrepancy located in the available records."


class ReconciliationState(TypedDict):
    # `messages` is the agent loop; `exception_id`/`payment_id` are carried as context for the
    # tools and the HITL node. Not multi-writer, so no reducer needed beyond `add_messages`.
    messages: Annotated[list, add_messages]
    exception_id: str
    payment_id: str


def build_reconciliation_agent(model: Any, db: Any, checkpointer: Any):
    """Build the Reconciliation Agent as a raw StateGraph with a HITL approval gate.

    `create_agent` is a tool-calling loop with no place for an `interrupt()`. The
    reconciliation flow needs a custom control flow: investigate (tool loop) → approval
    (interrupt, pause for operator review) → END. So this is a raw `StateGraph`, not a
    `create_agent` — exactly the LangGraph skill's "use raw StateGraph when control flow is
    NOT 'LLM picks a tool in a loop'" case.
    """
    tools = _build_tools(db)
    model_bound = model.bind_tools(tools)
    tool_node = ToolNode(tools)

    def investigate(state: ReconciliationState) -> dict:
        response = model_bound.invoke(state["messages"])
        return {"messages": [response]}

    def after_investigate(state: ReconciliationState) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        # No more tool calls — investigation finished (record_investigation has written
        # `exceptions.agent{}`). Route to the human-in-the-loop approval gate.
        return "approval"

    def approval(state: ReconciliationState) -> dict:
        # Read back the investigation the agent recorded. Idempotent — safe to re-run on
        # resume (the node re-runs from the top; the read repeats but changes nothing).
        doc = db["exceptions"].find_one(
            {"exceptionId": state["exception_id"]}, {"_id": 0, "agent": 1}
        ) or {}
        agent_block = doc.get("agent") or {}
        # Pause for operator review of the AI recommendation. The resume value is the
        # operator's acknowledgement — any truthy value means "reviewed". The operator then
        # resolves through the EXISTING transactions UI; this gate reviews, it does not
        # execute, so there is no duplicate resolve path (spec L1323).
        interrupt({
            "exceptionId": state["exception_id"],
            "rootCause": agent_block.get("rootCause"),
            "confidence": agent_block.get("confidence"),
            "recommendedResolution": agent_block.get("recommendedResolution"),
        })
        return {}  # reached only on resume → fall through to END

    builder = StateGraph(ReconciliationState)
    builder.add_node("investigate", investigate)
    builder.add_node("tools", tool_node)
    builder.add_node("approval", approval)
    builder.add_edge(START, "investigate")
    builder.add_conditional_edges(
        "investigate", after_investigate, {"tools": "tools", "approval": "approval"}
    )
    builder.add_edge("tools", "investigate")
    builder.add_edge("approval", END)
    return builder.compile(checkpointer=checkpointer)


def investigate(
    agent: Any, db: Any, exception_id: str, payment_id: str
) -> Optional[dict]:
    """Run the reconciliation agent for one OPEN discrepancy exception.

    Returns the `exceptions.agent{}` subdoc that was recorded (read back), or None if the
    agent failed or recorded nothing. Never raises — a failure leaves the exception's `agent`
    field unset and the operator investigates manually, exactly as before Phase 2.
    """
    user_msg = (
        f"Reconciliation mismatch on payment {payment_id} (exception {exception_id}). "
        "Investigate the discrepancy across the full payment lifecycle and record your "
        "findings with `record_investigation`."
    )
    try:
        agent.invoke(
            {
                "messages": [{"role": "user", "content": user_msg}],
                "exception_id": exception_id,
                "payment_id": payment_id,
            },
            config={"configurable": {"thread_id": exception_id}},
        )
    except Exception:  # noqa: BLE001 — never propagate to the worker.
        logger.warning(
            "reconciliation agent invoke failed for %s — leaving exception unchanged.",
            exception_id, exc_info=True,
        )
        return None
    # Read back what the agent recorded (if anything).
    try:
        doc = db["exceptions"].find_one({"exceptionId": exception_id}, {"_id": 0, "agent": 1})
    except Exception:  # noqa: BLE001
        return None
    return (doc or {}).get("agent")
