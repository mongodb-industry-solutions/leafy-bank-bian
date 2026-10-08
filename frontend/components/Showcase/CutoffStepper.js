"use client";

// Part D — the cut-off step machine. One 3s tick re-reads the payment and its newest cutoff
// case; a 1s tick drives the business clock. Every presenter action is followed by a sweep,
// fired without awaiting (the backend runs the LLM inline) and never more than one at a time.

import { useEffect, useMemo, useRef, useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Button from "@leafygreen-ui/button";
import { H2, Body } from "@leafygreen-ui/typography";
import { usePaymentWorkflow } from "@/lib/api/hooks";
import { closeOutFor, cutoffActOf, cutoffActsFor, cutoffByKey, cutoffStepsFor } from "./cutoffScenarios";
import {
  approveAsRaj, cutoffError, fastForward, findCase, moveClock, startCutoffScenario, sweepNow,
} from "./cutoffActions";
import StoryBar from "./StoryBar";
import StatusRail from "./StatusRail";
import { useAgentSteps } from "./AgentThinking";
import CutoffScene, { OUTCOME_LABEL, RISK_VARIANT } from "./scenes/CutoffScene";
import styles from "./Showcase.module.css";

const TICK_MS = 3000;
const FINAL_ACT = 6;
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
  const [note, setNote] = useState(null);
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

  // A failed approved action sends the agent back once; its new proposal needs its own decision.
  const revisedProposal =
    step.final && ctx.decision === "APPROVE" && !!agent?.proposedAction && caseDoc?.status === "AWAITING_APPROVAL";

  const businessMs = now + (ctx.offsetSeconds || 0) * 1000;
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

  async function guarded(fn) {
    if (running) return;
    setRunning(true);
    setError(null);
    try {
      const n = await fn();
      if (n) setNote(n);
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
    const ok = await guarded(() => runAction(step.run));
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
    setNote(decision === "APPROVE" ? "Approved. The agent executed the action and is verifying it." : "Rejected. The run has ended.");
    setIndex(steps.length - 1);
    setTick((t) => t + 1);
  }

  const acts = cutoffActsFor(scenarioKey);
  // Until the proposal lands, the decision step still shows the agent working (act 3).
  const currentAct = step.final && done && !revisedProposal ? FINAL_ACT
    : step.decide && waiting ? 3
    : cutoffActOf(step);
  const sceneAct = Math.min(currentAct, 5);
  const act = acts.find((a) => a.id === sceneAct) || acts[0];
  const riskLevel = caseDoc?.risk?.riskLevel;
  const expected = result === scenario.expectedOutcome;
  // One tone for the whole page, so the story bar, scene stripe and status card agree.
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
    onFastForward: () => guarded(closeOut),
  };

  const showButton = !step.final && !step.decide && !!step.button;

  const chips = [
    <Badge key="status" variant="lightgray">{payment?.status || ctx.init?.status || "—"}</Badge>,
    riskLevel && <Badge key="risk" variant={RISK_VARIANT[riskLevel] || "lightgray"}>{riskLevel.replaceAll("_", " ")}</Badge>,
    caseDoc && (
      <Badge key="case" variant={result ? "green" : caseDoc.status === "AWAITING_APPROVAL" ? "yellow" : "blue"}>
        {result ? OUTCOME_LABEL[result] || result : `Case ${caseDoc.status.replaceAll("_", " ").toLowerCase()}`}
      </Badge>
    ),
  ].filter(Boolean);

  return (
    <div className={`${styles.walkthrough} ${styles[`tone_${tone}`]}`}>
      <StoryBar acts={acts} currentAct={currentAct} tone={tone} />

      <div className={styles.stage}>
        <section className={`${styles.scene} ${styles.toneStripe}`}>
          <div className={styles.sceneHead}>
            <div>
              <H2 className={styles.narrationTitle}>{act.label}</H2>
              <Body className={styles.muted}>{act.caption}</Body>
            </div>
            <div className={styles.narrationActions}>
              {showButton && (
                <Button variant="primary" size="large" disabled={running || !!waiting} onClick={next}>
                  {running ? "Running…" : step.button}
                </Button>
              )}
              {waiting && <Body className={styles.muted}>{waiting}</Body>}
              <Button size="small" variant="default" onClick={onReset}>
                All scenarios
              </Button>
            </div>
          </div>

          {paymentId && (
            <div className={styles.presenterBar}>
              <Button size="small" disabled={sweeping || done} onClick={() => sweep(paymentId)}>
                {sweeping ? "Agent running…" : "Sweep now"}
              </Button>
            </div>
          )}

          {note && <Body className={styles.note}>{note}</Body>}
          {error && <Body className={styles.error}>{error}</Body>}

          <CutoffScene
            act={sceneAct}
            scenario={scenario}
            init={ctx.init}
            businessMs={businessMs}
            caseDoc={caseDoc}
            steps={agentSteps}
            started={started}
            canDecide={(step.decide && !ctx.decision) || revisedProposal}
            onDecided={onDecided}
            rejected={rejected}
            waiting={waiting}
            revisedProposal={revisedProposal}
            closing={closingProps || null}
          />
        </section>

        <StatusRail scenario={scenario} init={ctx.init} payment={payment} exceptions={[]} chips={chips} />
      </div>
    </div>
  );
}
