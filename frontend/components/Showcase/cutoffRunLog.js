// The cut-off walkthrough's run log: the same event model as runLog.js, derived from the case
// document and the payment the stepper already polls. Keys are stable, so a re-poll never adds
// a second line.

import { ACTION_LABEL } from "./agentPhrases";
import { agentStepEvents, DECISION, DETECTION, INVESTIGATION, PAYMENT, RESOLUTION } from "./runLog";
import { OUTCOME_LABEL } from "./scenes/CutoffScene";

const label = (value) => String(value).replaceAll("_", " ").toLowerCase();

const PROPOSAL_LABEL = {
  EXPEDITE: "Expedite to Fedwire",
  HOLD_NEXT_VALUE_DATE: "Hold for the next value date",
  DEFER_NEXT_BUSINESS_DAY: "Defer to the next business day",
};

// Actions the agent may take without asking (backend `cutoff_rules.AUTO`).
const AUTO_ACTIONS = new Set([
  "SEND_APPROVAL_REMINDER", "ESCALATE_TO_BACKUP_APPROVER", "RAISE_SCREENING_PRIORITY", "NOTIFY_CUSTOMER_FUNDS",
]);
const RESULT_TONE = { DONE: "good", REFUSED: "wait", ERROR: "alert" };

// Payment statuses reached on the way out, in order.
const CLOSING = [
  { status: "SUBMITTED", title: "Wire submitted to Fedwire" },
  { status: "SETTLED", title: "Payment settled" },
  { status: "RECONCILED", title: "Payment reconciled" },
];

// Each lifecycle state in plain words: the stage the wire is in, and for the four holds why it
// cannot go further. A state not listed falls back to its lowercased name.
export const STATE_INFO = {
  INITIATED: { stage: "Initiation", title: "Wire received and recorded" },
  VALIDATED: { stage: "Validation", title: "Validation passed" },
  FINAL_VALIDATED: { stage: "Validation", title: "Final checks passed" },
  ROUTED: { stage: "Orchestration", title: "Routed to Fedwire" },
  AUTHORISED: { stage: "Orchestration", title: "Authorised to send" },
  PENDING_APPROVAL: {
    stage: "Authentication and authorization", title: "Waiting for a second signature",
    why: "The wire needs a second signatory and the assigned approver is not available.",
  },
  PENDING_FUNDS: {
    stage: "Validation", title: "Waiting for funds",
    why: "The account holds less than the wire needs, so it cannot be reserved.",
  },
  CUTOFF_EXCEPTION: {
    stage: "Orchestration", title: "Past the internal cut-off",
    why: "The wire missed the bank's internal cut-off and needs a decision.",
  },
  PENDING_SCREENING: {
    stage: "Orchestration", title: "Waiting in the screening queue",
    why: "A possible sanctions match is waiting for an analyst.",
  },
};
const HOLD_DWELL_MS = 4000;

// The wire's own journey, from its lifecycle events: each move is a line, and the first hold
// is a card of its own that says what blocks the wire. Moves after the hold belong to the
// resolution. Submitted, settled and reconciled are covered by CLOSING.
function paymentEvents(payment) {
  const out = [];
  const closing = new Set(CLOSING.map((c) => c.status));
  let held = false;
  (payment?.lifecycle?.events || []).forEach((e, i) => {
    if (closing.has(e.state)) return;
    const info = STATE_INFO[e.state] || { stage: label(e.state), title: `Moved to ${label(e.state)}` };
    const hold = !!info.why;
    const base = held ? { phase: RESOLUTION, ord: 640 + i / 100 } : { phase: PAYMENT, ord: 10 + i };
    out.push({
      key: `state:${i}:${e.state}`, ...base, lane: "code",
      tone: hold ? "wait" : undefined, title: `${info.stage}: ${info.title}`, detail: e.reason,
    });
    if (hold && !held) {
      out.push({
        key: `stall:${i}`, phase: PAYMENT, lane: "code", ord: 10 + i + 0.5, card: true, tone: "alert", stall: true,
        dwell: HOLD_DWELL_MS,
        title: `Stalled at ${info.stage}`,
        detail: `${info.why} Nothing moves it forward on its own. Code alone would leave it here until the cut-off passes.`,
      });
    }
    if (hold) held = true;
  });
  return out;
}

const detailText = (detail) => {
  if (detail == null) return undefined;
  if (typeof detail === "string") return detail;
  return Object.entries(detail).map(([k, v]) => `${k} ${v}`).join(", ");
};

export function deriveCutoffEvents({ scenario, ctx, payment, agentSteps, caseDoc }) {
  const out = [...paymentEvents(payment)];
  const agent = caseDoc?.agent;
  const risk = caseDoc?.risk;

  if (caseDoc) {
    out.push({
      key: "case", phase: DETECTION, lane: "mongo", ord: 100,
      title: "A case opened for the held wire", detail: caseDoc.caseId,
    });
  }
  if (risk) {
    out.push({
      key: `risk:${risk.riskLevel}:${risk.phase}`, phase: DETECTION, lane: "code", ord: 110, card: true,
      tone: risk.riskLevel === "ON_TRACK" ? "good" : risk.riskLevel === "WILL_MISS" ? "alert" : "wait",
      tag: "$percentile",
      title: `Risk computed by code: ${label(risk.riskLevel)}`,
      detail: risk.remainingP90Min != null
        ? `The current stage typically clears in ${risk.remainingP50Min ?? "—"} min (p50), ${risk.remainingP90Min} min (p90).`
        : undefined,
      items: risk.reasons || [],
    });
  }

  out.push(...agentStepEvents(agentSteps, scenario.bank));

  (agent?.actionsTaken || []).forEach((a, i) => {
    const own = AUTO_ACTIONS.has(a.action);
    out.push({
      key: `action:${i}:${a.action}`, phase: own ? INVESTIGATION : RESOLUTION, lane: "agent",
      ord: own ? 410 + i : 600 + i, tone: own ? RESULT_TONE[a.result] : undefined,
      title: own ? `Acted on its own: ${ACTION_LABEL[a.action] || label(a.action)}` : `Executed ${label(a.action || "action")}`,
      detail: detailText(a.detail) ?? (a.result && own ? label(a.result) : undefined),
    });
  });

  if (agent?.assessedAt) {
    out.push({
      key: "assessment", phase: INVESTIGATION, lane: "agent", ord: 400, card: true, tone: "good",
      title: `Assessment${agent.blocker ? `: ${label(agent.blocker)}` : ""}${agent.confidence ? `, confidence ${agent.confidence}` : ""}`,
      detail: agent.diagnosis,
      items: (agent.evidence || []).map((ev) => (typeof ev === "string" ? ev : JSON.stringify(ev))),
    });
  }

  const proposal = agent?.proposedAction;
  if (proposal) {
    out.push({
      key: "proposal", phase: INVESTIGATION, lane: "agent", ord: 420, tone: "wait",
      title: `Proposed ${PROPOSAL_LABEL[proposal.action] || label(proposal.action)}`, detail: proposal.rationale,
    });
    out.push({
      key: "policy", phase: INVESTIGATION, lane: "code", ord: 430, card: true, title: "Policy check",
      checks: [{ text: `${proposal.action} moves the wire, so it waits for a human. The agent cannot approve it.`, ok: true }],
      detail: proposal.route ? `Approving calls ${proposal.route}` : undefined,
    });
    out.push({
      key: "paused", phase: DECISION, lane: "mongo", ord: 440, tone: "wait", tag: "LangGraph checkpoint",
      title: "Run paused for approval. State saved in MongoDB, resumable after a restart",
    });
  }

  if (ctx.decision) {
    out.push({
      key: `decision:${ctx.decision}`, phase: DECISION, lane: "human", ord: 450,
      tone: ctx.decision === "APPROVE" ? "good" : "alert",
      title: ctx.decision === "APPROVE" ? "You approved the proposal" : "You rejected the proposal",
      detail: "Recorded on the case",
    });
  }
  if (agent?.verification?.result) {
    out.push({
      key: `verify:${agent.verification.result}`, phase: RESOLUTION, lane: "code", ord: 700, tone: "good",
      title: `Agent re-read the payment: ${label(agent.verification.result)}`,
    });
  }
  const outcome = caseDoc?.outcome;
  if (outcome?.result) {
    const expected = outcome.result === scenario.expectedOutcome;
    out.push({
      key: `outcome:${outcome.result}`, phase: RESOLUTION, lane: "code", ord: 710, tone: expected ? "good" : "wait",
      title: `Outcome: ${OUTCOME_LABEL[outcome.result] || label(outcome.result)}`,
      detail: outcome.valueDate ? `Value date ${outcome.valueDate}` : undefined,
    });
  }

  const reached = CLOSING.findIndex((c) => c.status === (payment?.status === "COMPLETED" ? "SETTLED" : payment?.status));
  CLOSING.slice(0, reached + 1).forEach((c, i) => {
    out.push({ key: `pay:${c.status}`, phase: RESOLUTION, lane: "code", ord: 720 + i, tone: "good", title: c.title });
  });

  if (agent?.error) {
    out.push({
      key: "agent-error", phase: INVESTIGATION, lane: "agent", ord: 900, tone: "alert",
      title: "Agent error", detail: agent.error.message,
    });
  }
  return out;
}
