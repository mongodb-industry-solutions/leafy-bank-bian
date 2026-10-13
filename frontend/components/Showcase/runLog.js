// The reconciliation walkthrough's progress model: one chronological log of events. Every
// line is derived from data the stepper already polls (payment, trace, exception, agent
// steps) or pushed when the presenter acts, so a re-poll never duplicates a line and a tool
// call fills in its result when it arrives.
//
// Event: { key, phase, lane, title, detail?, tone?, tag?, expand?, items?, writes? }
//   lane   agent | code | mongo | human
//   tag    the MongoDB feature at work, from briefs.momentsFor
//   expand raw text revealed on click (a tool's result); items a short bullet list
//   writes the step's documents (stepWrites), shown through WriteRow

import { useState } from "react";
import { momentsFor } from "./briefs";
import { phraseFor } from "./agentPhrases";
import { writesFor } from "./stepWrites";
import { gapOf, major, minor, railLegOf } from "./scenes/format";

export const PHASES = ["The payment", "Detection", "Agent investigation", "Human decision", "Resolution"];
const [PAYMENT, DETECTION, INVESTIGATION, DECISION, RESOLUTION] = PHASES;

export const LANE_LABEL = { agent: "Agent", code: "Code", mongo: "MongoDB", human: "You" };

const PHASE_OF_STEP = {
  initiate: PAYMENT, settle: PAYMENT, glpost: PAYMENT,
  statement: DETECTION, match: DETECTION, overdue: DETECTION, window: DETECTION,
  investigate: INVESTIGATION, approve: DECISION, late: RESOLUTION, verify: RESOLUTION,
};
// Where a step's MongoDB writes sit among the detection and agent lines of the same phase.
// Steps not listed sort by their position in the step list.
const WRITES_ORD = { investigate: 250, approve: 445, late: 500, verify: 800 };

export const phaseOfStep = (stepKey) => PHASE_OF_STEP[stepKey] || PAYMENT;

const label = (value) => String(value).replaceAll("_", " ").toLowerCase();

function shortArgs(args) {
  if (!args || typeof args !== "object") return "";
  const text = Object.entries(args)
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
    .join(", ");
  return text.length > 80 ? `${text.slice(0, 77)}…` : text;
}

/**
 * Policy lines come only from what the backend recorded: the agent's cause, the scenario's
 * rule, and the proposed action. A proposal that exists has already passed `check_proposal`.
 * `ok: false` marks a refusal the scenario is designed around (R4's FEE).
 */
export function policyLines(agent, scenario, gapMajor) {
  if (!agent?.cause) return [];
  const { cause, constraint, action } = scenario.decision;
  const refused = String(constraint.code).endsWith("refused");
  const lines = [
    { text: `Cause ${agent.cause}: ${cause.label}`, ok: true },
    { text: `Rule: ${constraint.label}`, ok: !refused },
  ];
  const proposal = agent.proposedAction;
  if (proposal) {
    lines.push({ text: `${proposal.action} is the allowed action: ${action.label}`, ok: true });
    const amount = proposal.params?.amount;
    if (amount != null && gapMajor != null) {
      lines.push({
        text: `Adjustment ${major(amount)} ${Number(amount) === gapMajor ? "equals" : "differs from"} the gap ${major(gapMajor)}`,
        ok: Number(amount) === gapMajor,
      });
    }
    const target = proposal.params?.target;
    if (target) {
      lines.push({ text: `Linked only to a returned candidate: ${target.reference}${target.score != null ? ` (score ${target.score})` : ""}`, ok: true });
    }
  } else if (agent.nextCheckAt || agent.recheckCount) {
    lines.push({ text: `${action.code} moves no money, so it ran without asking anyone`, ok: true });
  }
  return lines;
}

/** One event per tool call, its result attached once the matching tool_result arrives. */
function agentStepEvents(agentSteps, bank) {
  const out = [];
  const open = [];
  // Reasoning is the agent explaining the call that follows, so it rides on that call.
  let thought = null;
  agentSteps.forEach((s, i) => {
    if (s.kind === "tool_call") {
      const ev = {
        key: `call:${i}`, phase: INVESTIGATION, lane: "agent", ord: 300 + i,
        title: phraseFor(s.tool, bank), detail: `${s.tool}(${shortArgs(s.args)})`,
        thought: thought?.slice(0, 220),
      };
      thought = null;
      out.push(ev);
      open.push({ tool: s.tool, ev });
    } else if (s.kind === "tool_result") {
      const slot = open.findIndex((o) => o.tool === s.tool);
      if (slot >= 0) open.splice(slot, 1)[0].ev.expand = String(s.text || "");
    } else if (s.kind !== "note" && s.text) {
      if (thought) out.push({ key: `think:${i}`, phase: INVESTIGATION, lane: "agent", ord: 300 + i - 0.5, title: "Reasoning", detail: thought.slice(0, 220) });
      thought = String(s.text);
    }
  });
  if (thought) out.push({ key: "think:last", phase: INVESTIGATION, lane: "agent", ord: 300 + agentSteps.length, title: "Reasoning", detail: thought.slice(0, 220) });
  return out;
}

/** Everything the current data implies, oldest-meaning first. Keys are stable. */
export function deriveRunEvents({ scenario, scenarioKey, steps, index, step, ctx, payment, trace, agentSteps, agentStarted, agent, proposal, exceptions, gapMajor }) {
  const out = [];
  const bank = scenario.bank;

  // The MongoDB writes of each step that has run; a gated or final step writes while on screen.
  const lastWritten = step.gate || step.final ? index : index - 1;
  steps.slice(0, lastWritten + 1).forEach((s, si) => {
    const writes = writesFor(s.key, scenarioKey);
    if (!writes.length) return;
    const moment = momentsFor(scenarioKey, s.key)[0];
    out.push({
      key: `writes:${s.key}`, phase: phaseOfStep(s.key), lane: "mongo", ord: WRITES_ORD[s.key] ?? si,
      stepKey: s.key, title: s.title,
      detail: moment?.text, tag: moment?.feature, writes,
    });
  });

  const leg = railLegOf(trace);
  const gap = gapOf(leg);
  if (gap != null) {
    out.push({
      key: "gap", phase: DETECTION, lane: "code", ord: 100, tone: gap ? "alert" : "good",
      title: gap
        ? `Books say ${minor(leg.leftAmount)}, ${bank} statement says ${minor(leg.rightAmount)}: gap of ${minor(gap)}`
        : "Books and statement agree",
    });
  }

  exceptions.forEach((e) => {
    out.push({
      key: `exc:${e.exceptionId}`, phase: DETECTION, lane: "code", ord: 101, at: e.createdAt ? Date.parse(e.createdAt) : undefined,
      tone: "alert", tag: "Change stream",
      title: `Exception opened: ${label(e.category || "unknown")}`, detail: e.exceptionId,
    });
    if (e.status && e.status !== "OPEN") {
      out.push({ key: `exc:${e.exceptionId}:${e.status}`, phase: RESOLUTION, lane: "code", ord: 650, tone: "good", title: `Exception ${label(e.status)}` });
    }
  });

  if (agentStarted) {
    out.push({
      key: "woke", phase: INVESTIGATION, lane: "mongo", ord: 200, tag: "Change stream",
      title: "The new exception woke the agent", detail: "Insert on exceptions, picked up through a change stream",
    });
  }
  out.push(...agentStepEvents(agentSteps, bank));

  if (agent?.cause) {
    out.push({
      key: "finding", phase: INVESTIGATION, lane: "agent", ord: 400, card: true, tone: "good", tag: "Embedded agent{}",
      title: `Finding: ${agent.cause}${agent.confidence ? `, confidence ${agent.confidence}` : ""}`,
      detail: agent.rootCause,
      items: (agent.evidence || []).map((ev) => (typeof ev === "string" ? ev : JSON.stringify(ev))),
    });
  }
  const recheckOnly = (agent?.nextCheckAt || agent?.recheckCount > 0) && !agent?.proposedAction;
  if (recheckOnly) {
    out.push({
      key: `recheck:${agent.recheckCount ?? 0}`, phase: INVESTIGATION, lane: "agent", ord: 410, tone: "good",
      title: `Acted without approval: RECHECK ${agent.recheckCount ?? 0} of 3 scheduled`,
      detail: "Re-checking moves no money and changes no books.",
    });
  }

  const proposed = agent?.proposedAction || proposal;
  if (proposed) {
    out.push({
      key: "proposal", phase: INVESTIGATION, lane: "agent", ord: 420, tone: "wait",
      title: `Proposed ${scenario.decision.action.label}`, detail: proposed.action,
    });
    const lines = policyLines({ ...agent, proposedAction: proposed }, scenario, gapMajor);
    if (lines.length) {
      out.push({
        key: "policy", phase: INVESTIGATION, lane: "code", ord: 430, card: true, title: "Policy check",
        checks: lines,
      });
    }
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
      detail: "Stored on the exception as agent.approval",
    });
  }
  (agent?.actionsTaken || []).forEach((a, i) => {
    out.push({
      key: `action:${i}:${a.action}`, phase: RESOLUTION, lane: "agent", ord: 600 + i,
      title: `Executed ${label(a.action || "action")}`, detail: typeof a.result === "string" ? a.result : undefined,
    });
  });
  if (agent?.verification?.result) {
    out.push({
      key: `verify:${agent.verification.result}`, phase: RESOLUTION, lane: "code", ord: 700, tone: "good",
      title: `Agent re-read the system: ${label(agent.verification.result)}`,
    });
  }
  if (payment?.lifecycle?.reconciliationStatus === "RECONCILED") {
    out.push({ key: "reconciled", phase: RESOLUTION, lane: "code", ord: 710, tone: "good", title: "Payment reconciled" });
  }
  if (agent?.error && !agent?.verification) {
    out.push({ key: "agent-error", phase: INVESTIGATION, lane: "agent", ord: 900, tone: "alert", title: "Agent error", detail: agent.error.message });
  }
  return out;
}

/**
 * The log, sorted by phase and then by each line's logical position (`ord`), never by when
 * the browser first saw it. A time is shown only where the data has one: a stored timestamp,
 * or the click that ran a step. `push` adds a line only the stepper knows about; one that
 * reports a step's result takes over the title of that step's MongoDB-writes line instead of
 * printing a second row.
 */
export function useRunLog(sources) {
  const [manual, setManual] = useState([]);

  const derived = deriveRunEvents(sources);
  const merged = new Set();
  manual.forEach((m) => {
    const target = m.stepKey && derived.find((d) => d.key === `writes:${m.stepKey}`);
    if (!target) return;
    target.title = m.title;
    target.at = m.at;
    merged.add(m.key);
  });
  const all = [...derived, ...manual.filter((m) => !merged.has(m.key))];
  const stepOrd = (e) => e.ord ?? sources.steps.findIndex((s) => s.key === e.stepKey);
  const events = all
    .map((e, i) => ({ e, i }))
    .sort((a, b) => PHASES.indexOf(a.e.phase) - PHASES.indexOf(b.e.phase) || stepOrd(a.e) - stepOrd(b.e) || a.i - b.i)
    .map(({ e }) => e);

  const push = (event) =>
    setManual((prev) => (prev.some((e) => e.key === event.key) ? prev : [...prev, { ...event, at: Date.now() }]));
  return { events, push };
}
