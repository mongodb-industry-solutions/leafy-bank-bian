"use client";

// Part D — the cut-off step machine. One 3s tick re-reads the payment and its newest cutoff
// case; a 1s tick drives the business clock. Every presenter action is followed by a sweep,
// fired without awaiting (the backend runs the LLM inline) and never more than one at a time.

import { useEffect, useMemo, useRef, useState } from "react";
import Badge from "@leafygreen-ui/badge";
import { Body, Overline } from "@leafygreen-ui/typography";
import { usePaymentWorkflow } from "@/lib/api/hooks";
import { closeOutFor, cutoffByKey, cutoffStepsFor } from "./cutoffScenarios";
import {
  approveAsRaj, cutoffError, fastForward, findCase, moveClock, startCutoffScenario, sweepNow,
} from "./cutoffActions";
import { BriefStrip } from "./ScenarioBrief";
import { PaymentCard } from "./StatusRail";
import RunConsole from "./RunConsole";
import { deriveActivity } from "./activity";
import { useAgentSteps } from "./AgentThinking";
import { INVESTIGATION, PAYMENT, RESOLUTION, usePacedEvents, useRunLog } from "./runLog";
import { deriveCutoffEvents } from "./cutoffRunLog";
import { ClockStrip, ClosingPanel, Outcome, Proposal, OUTCOME_LABEL, RISK_VARIANT } from "./scenes/CutoffScene";
import styles from "./Showcase.module.css";

const TICK_MS = 3000;
const CLOSE_TIMEOUT_MS = 90000;
const SETTLED_STATUSES = ["SETTLED", "RECONCILED", "COMPLETED"];

/** The newest cutoff case for the payment, re-read on the shared tick. */
function useCutoffCase(paymentId, tick) {
  const [caseDoc, setCaseDoc] = useState(null);
  useEffect(() => {
    if (!paymentId) return undefined;
    let cancelled = false;
    findCase(paymentId).then(({ data, error }) => {
      if (!cancelled && !error && data) setCaseDoc(data);
    });
    return () => {
      cancelled = true;
    };
  }, [paymentId, tick]);
  return [caseDoc, setCaseDoc];
}

/** Fire-and-forget sweeps, one in flight; a request made meanwhile runs once when it ends. */
function useSweeper(onError, onDone) {
  const state = useRef({ inFlight: false, again: null, mounted: true });
  const [sweeping, setSweeping] = useState(false);
  useEffect(() => {
    const s = state.current;
    s.mounted = true;
    return () => {
      s.mounted = false;
    };
  }, []);

  function sweep(paymentId) {
    const s = state.current;
    if (!paymentId) return;
    if (s.inFlight) {
      s.again = paymentId;
      return;
    }
    s.inFlight = true;
    setSweeping(true);
    sweepNow(paymentId).then(({ error }) => {
      s.inFlight = false;
      if (!s.mounted) return;
      if (error) onError(cutoffError(error));
      onDone();
      const next = s.again;
      s.again = null;
      if (next) sweep(next);
      else setSweeping(false);
    });
  }
  return [sweep, sweeping];
}

// Where each presenter action lands in the run log. Actions taken mid-investigation sit just
// after the agent steps seen so far.
function logSlot(kind, stepsSeen) {
  const after = 300 + stepsSeen;
  return {
    start: { key: "start", phase: PAYMENT, lane: "code", ord: 0 },
    clock: { key: "clock", phase: INVESTIGATION, lane: "code", ord: after - 0.25 },
    approveRaj: { key: "approveRaj", phase: INVESTIGATION, lane: "human", ord: after - 0.2 },
    closeOut: { key: "closeOut", phase: RESOLUTION, lane: "human", ord: 715 },
  }[kind];
}

function gateWaiting(gate, { agent, caseDoc, ctx }) {
  if (!gate) return null;
  if (!caseDoc) return "Waiting for the agent to open a case…";
  if (gate === "assessed") return agent?.assessedAt || agent?.error ? null : "Agent assessing…";
  if (gate === "proposal") return agent?.proposedAction ? null : "Agent assessing…";
  return null;
}

export default function CutoffStepper({ scenarioKey, onReset }) {
  const scenario = cutoffByKey(scenarioKey);
  const steps = useMemo(() => cutoffStepsFor(scenarioKey), [scenarioKey]);
  const [index, setIndex] = useState(0);
  const [ctx, setCtx] = useState({});
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [tick, setTick] = useState(0);
  const [now, setNow] = useState(Date.now());

  const step = steps[index];
  const paymentId = ctx.paymentId || null;

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), TICK_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const { payment } = usePaymentWorkflow(paymentId, tick, 0);
  const [caseDoc, setCaseDoc] = useCutoffCase(paymentId, tick);
  const agent = caseDoc?.agent || null;
  const [sweep, sweeping] = useSweeper(setError, () => setTick((t) => t + 1));

  const rejected = ctx.decision === "REJECT";
  const closing = closeOutFor(scenarioKey);
  const result = caseDoc?.outcome?.result || null;
  const done = rejected || !!result;
  const { steps: agentSteps, started } = useAgentSteps(caseDoc?.caseId || null, !done, "cutoff/cases");
  const { events: allEvents, push: pushEvent } = useRunLog(
    { scenario, steps, ctx, payment, agentSteps, caseDoc },
    deriveCutoffEvents
  );

  // Lines appear one at a time, so the presenter can read where the wire is and where it stops.
  const { events, backlog } = usePacedEvents(allEvents);

  // A failed approved action sends the agent back once; its new proposal needs its own decision.
  const revisedProposal =
    step.final && ctx.decision === "APPROVE" && !!agent?.proposedAction && caseDoc?.status === "AWAITING_APPROVAL";

  const businessMs = now + (ctx.offsetSeconds || 0) * 1000;
  // The clock stops where the run ended, so the strip keeps showing when the outcome landed.
  const frozenMs = useRef(null);
  if (!done) frozenMs.current = null;
  else if (frozenMs.current == null) frozenMs.current = businessMs;
  const displayMs = frozenMs.current ?? businessMs;
  const waiting = step.final
    ? revisedProposal ? "The agent revised its proposal. Approve or reject it" : done ? null : "Waiting for the outcome…"
    : gateWaiting(step.gate, { agent, caseDoc, ctx });

  async function runAction(kind) {
    if (kind === "start") {
      const { data, error: err } = await startCutoffScenario(scenarioKey);
      if (err) throw new Error(cutoffError(err));
      setCtx({ runId: data.runId, offsetSeconds: data.offsetSeconds, paymentId: data.paymentId, init: data });
      sweep(data.paymentId);
      return data.warning ? `Wire ${data.paymentId} is ${data.status}. ${data.warning}` : `Wire ${data.paymentId} is held at ${data.status}.`;
    }
    if (kind === "clock") return advanceClock(scenario.presenter.clock);
    if (kind === "approveRaj") {
      const { error: err } = await approveAsRaj(paymentId);
      if (err) throw new Error(cutoffError(err));
      sweep(paymentId);
      return "Raj approved the wire as the backup signatory.";
    }
    return null;
  }

  async function advanceClock(anchor) {
    const { data, error: err } = await moveClock(ctx.runId, anchor);
    if (err) throw new Error(cutoffError(err));
    setCtx((c) => ({ ...c, offsetSeconds: data.offsetSeconds }));
    sweep(paymentId);
    return `Business clock moved to ${anchor} ET.`;
  }

  // Fast-forward the run: the case keeps the agent's decision, the payment moves on. The 3s
  // tick then re-reads the payment until it settles or the 90 s timeout message shows.
  async function closeOut() {
    const { data, error: err } = await fastForward(ctx.runId);
    if (err) throw new Error(cutoffError(err));
    const row = (data.payments || []).find((p) => p.paymentId === paymentId) || null;
    setCtx((c) => ({
      ...c,
      offsetSeconds: data.clock?.offsetSeconds ?? c.offsetSeconds,
      release: row,
      closeStartedAt: row?.error ? null : Date.now(),
    }));
    if (row?.error) throw new Error(row.error);
    return closing.note;
  }

  async function guarded(fn, kind) {
    if (running) return;
    setRunning(true);
    setError(null);
    try {
      const n = await fn();
      if (n) pushEvent({ ...logSlot(kind, agentSteps.length), title: n });
      setTick((t) => t + 1);
      return true;
    } catch (e) {
      setError(e.message || String(e));
      return false;
    } finally {
      setRunning(false);
    }
  }

  async function next() {
    if (running || waiting || step.final || step.decide) return;
    const ok = await guarded(() => runAction(step.run), step.run);
    if (ok) setIndex((i) => Math.min(i + 1, steps.length - 1));
  }

  function onDecided(decision, updated) {
    // A watch-only scenario is already settling once approved: the 90 s clock starts now.
    setCtx((c) => ({
      ...c,
      decision,
      closeStartedAt: decision === "APPROVE" && scenario.closeOut === "watch" ? Date.now() : c.closeStartedAt,
    }));
    if (updated) setCaseDoc(updated);
    setIndex(steps.length - 1);
    setTick((t) => t + 1);
  }

  const riskLevel = caseDoc?.risk?.riskLevel;
  const expected = result === scenario.expectedOutcome;
  // One tone for the whole page, so the scene stripe and status card agree.
  const tone = done
    ? expected ? "resolved" : "waiting"
    : riskLevel === "WILL_MISS" ? "alert"
    : riskLevel === "AT_RISK" ? "waiting"
    : "neutral";

  const settled = SETTLED_STATUSES.includes(payment?.status);
  const closingVisible = !!paymentId && !rejected && (scenario.closeOut === "watch" ? ctx.decision === "APPROVE" : step.final && done);
  const closingProps = closingVisible && {
    copy: closing,
    mode: scenario.closeOut,
    payment,
    release: ctx.release,
    busy: running,
    timedOut: !!ctx.closeStartedAt && !settled && now - ctx.closeStartedAt > CLOSE_TIMEOUT_MS,
    onFastForward: () => guarded(closeOut, "closeOut"),
  };

  const canDecide = (step.decide && !ctx.decision) || revisedProposal;

  const gate = step.gate;
  const agentBusy =
    !!caseDoc && !done && !agent?.error && !revisedProposal &&
    ((gate === "assessed" && !agent?.assessedAt) || (gate === "proposal" && !agent?.proposedAction) ||
      (step.final && !closingProps));
  const activity = deriveActivity({
    humanWait: canDecide && agent?.proposedAction ? "Waiting for your decision" : null,
    running: running ? "Applying this step" : sweeping ? "Waking the agent" : null,
    externalWait: step.final && closingProps && ctx.closeStartedAt && !settled ? "Waiting for the wire to settle" : null,
    systemWait: gate && !caseDoc ? "Waiting for the agent to open a case" : null,
    agentBusy,
    agentSteps,
    bank: scenario.bank,
  });

  const chips = [
    <Badge key="status" variant="lightgray">{payment?.status || ctx.init?.status || "—"}</Badge>,
    riskLevel && <Badge key="risk" variant={RISK_VARIANT[riskLevel] || "lightgray"}>{riskLevel.replaceAll("_", " ")}</Badge>,
    caseDoc && (
      <Badge key="case" variant={result ? "green" : caseDoc.status === "AWAITING_APPROVAL" ? "yellow" : "blue"}>
        {result ? OUTCOME_LABEL[result] || result : `Case ${caseDoc.status.replaceAll("_", " ").toLowerCase()}`}
      </Badge>
    ),
  ].filter(Boolean);

  const controller = {
    label: step.button,
    final: !!step.final,
    showButton: !step.final && !step.decide && !!step.button,
    running,
    waiting,
    error,
    onNext: next,
    onReset,
    onSweep: paymentId ? () => sweep(paymentId) : null,
    sweeping,
    sweepDisabled: done,
  };

  return (
    <div className={`${styles.walkthrough} ${styles[`tone_${tone}`]}`}>
      <div className={styles.stageFull}>
        <RunConsole
          stepIndex={index}
          stepCards={steps.map((s) => ({
            key: s.key,
            title: s.final ? (done ? "Run complete" : "Verifying the result") : s.title,
            summary: s.summary,
            beat: scenario.beats?.[s.key],
          }))}
          top={(
            <>
              <BriefStrip scenario={scenario} />
              <PaymentCard scenario={scenario} init={ctx.init} payment={payment} exceptions={[]} chips={chips} />
            </>
          )}
          controller={controller}
          events={events}
          pending={backlog > 0 ? "Next event coming…" : null}
          activity={activity}
          evidence={ctx.init && <ClockStrip businessMs={displayMs} risk={caseDoc?.risk} init={ctx.init} />}
          failure={agent?.error && !agent?.verification && (
            <div className={styles.section}>
              <Overline>Assessment failed</Overline>
              <Body className={styles.error}>{agent.error.message}</Body>
              <Body className={styles.muted}>
                Attempt {agent.error.attempts}. If this is an expired AWS SSO session, run{" "}
                <span className={styles.mono}>aws sso login</span>, then use Sweep now.
              </Body>
            </div>
          )}
          review={agent?.proposedAction && canDecide && (
            <div className={styles.reviewPanel}>
              <Overline>Needs your decision</Overline>
              <Proposal caseId={caseDoc?.caseId} proposal={agent.proposedAction} canDecide={canDecide} onDecided={onDecided} />
            </div>
          )}
          result={step.final && !revisedProposal && (
            <div className={styles.section}>
              <Outcome scenario={scenario} caseDoc={caseDoc} rejected={rejected} waiting={waiting} />
              {closingProps && <ClosingPanel closing={closingProps} />}
            </div>
          )}
        />
      </div>
    </div>
  );
}
