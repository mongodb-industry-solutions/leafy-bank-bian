"use client";

// Plan E — the step machine. One owner for the scenario's polls: a single 3s tick re-reads
// the payment, its trace, the OPEN exceptions list and the followed exceptions' agent{}.
// AgentThinking owns only its own steps poll.

import { useEffect, useMemo, useState } from "react";
import Button from "@leafygreen-ui/button";
import Badge from "@leafygreen-ui/badge";
import Card from "@leafygreen-ui/card";
import Stepper, { Step } from "@leafygreen-ui/stepper";
import { H2, Body } from "@leafygreen-ui/typography";
import { agentApi, coreApi, pipelineApi } from "@/lib/api/client";
import { usePaymentWorkflow, usePipelineTrace, useWorkflowExceptions } from "@/lib/api/hooks";
import { CATEGORY, OVERDUE_SECONDS, scenarioByKey, stepsFor } from "./scenarios";
import PaymentTracker from "./PaymentTracker";
import AgentThinking from "./AgentThinking";
import styles from "./Showcase.module.css";

const TICK_MS = 3000;
const OWN_CATEGORIES = new Set([CATEGORY.DISCREPANCY, CATEGORY.MISSING]);

const generateStatement = (includeOrphan) =>
  coreApi("FinancialGateway/GW-WIRE-01/Statement/Generate", {
    method: "POST",
    body: { accountCode: "1111", includeOrphan },
  });

/** Runs one step's backend action. Returns { patch, note } or throws with the error text. */
async function runAction(kind, scenarioKey) {
  if (kind === "initiate") {
    const { data, error } = await coreApi("workflow/demo/recon-scenario", {
      method: "POST",
      body: { scenario: scenarioKey },
    });
    if (error) throw new Error(error);
    return {
      patch: { init: data, paymentId: data.paymentId },
      note: `Initiated ${data.paymentId} — ${data.status}.`,
    };
  }
  if (kind === "settle") {
    const { data, error } = await coreApi("workflow/demo/settle-due", { method: "POST" });
    if (error) throw new Error(error);
    return {
      patch: { settledAt: Date.now() },
      note: data?.settled ? `Settled ${data.settled} due wire(s).` : "Already settled — the settlement worker got there first.",
    };
  }
  if (kind === "batch") {
    const { error } = await pipelineApi("batch/trigger", null, { method: "POST" });
    if (error) throw new Error(error);
    return { patch: {}, note: "GL batch ran." };
  }
  if (kind === "statement" || kind === "statementLate") {
    const { data, error } = await generateStatement(scenarioKey === "R5");
    if (error) throw new Error(error);
    if (!data?.generated) {
      if (scenarioKey === "R5") throw new Error("No statement generated — nothing waiting to be booked.");
      return { patch: {}, note: "No new statement — the line was already booked (is ENABLE_STATEMENT_SIM off?)." };
    }
    const orphans = data.orphanCount ? `, ${data.orphanCount} unknown` : "";
    const patch = kind === "statementLate" ? {} : { statementId: data.paymentMessageId };
    return {
      patch,
      note: `Statement ${data.paymentMessageId} booked: ${data.entryCount} line(s)${orphans}.`,
    };
  }
  return { patch: {}, note: null };
}

/** Polls agent{} for each exception id on the shared tick. */
function useAgentRecords(ids, tick) {
  const [records, setRecords] = useState({});
  const key = ids.join(",");
  useEffect(() => {
    if (!key) return undefined;
    let cancelled = false;
    Promise.all(
      key.split(",").map((id) =>
        agentApi(`reconciliation/${encodeURIComponent(id)}`).then(({ data }) => [id, data?.agent || null])
      )
    ).then((pairs) => {
      if (!cancelled) setRecords((prev) => ({ ...prev, ...Object.fromEntries(pairs) }));
    });
    return () => {
      cancelled = true;
    };
  }, [key, tick]);
  return records;
}

export default function ScenarioStepper({ scenarioKey, onReset }) {
  const scenario = scenarioByKey(scenarioKey);
  const steps = useMemo(() => stepsFor(scenarioKey), [scenarioKey]);
  const [index, setIndex] = useState(0);
  const [ctx, setCtx] = useState({});
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [note, setNote] = useState(null);
  const [tick, setTick] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [orphan, setOrphan] = useState(null);
  const [lastProposals, setLastProposals] = useState({});

  const step = steps[index];

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), TICK_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!step.countdown) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [step.countdown]);

  const paymentId = ctx.paymentId || null;
  const { payment } = usePaymentWorkflow(paymentId, tick, 0);
  const { trace } = usePipelineTrace(paymentId, !!paymentId, 2000, tick);
  const needsOrphan = scenarioKey === "R2" || scenarioKey === "R5";
  const { items: openItems } = useWorkflowExceptions({ limit: 100 }, needsOrphan && ctx.statementId ? tick : -1);

  // R2/R5: the ORPHANED line on THIS scenario's statement. Kept once seen, because a LINK
  // resolves it and it drops out of the OPEN list.
  useEffect(() => {
    if (!needsOrphan || !ctx.statementId) return;
    const hit = (openItems || [])
      .map((row) => row.exception)
      .find((e) => e?.category === CATEGORY.ORPHANED && e?.subjectRef?.paymentMessageId === ctx.statementId);
    if (hit) setOrphan(hit);
    else setOrphan((prev) => (prev ? { ...prev, status: prev.status === "OPEN" ? "RESOLVED" : prev.status } : prev));
  }, [openItems, needsOrphan, ctx.statementId]);

  const ownException = useMemo(() => {
    const own = (payment?.exceptions || []).filter((e) => OWN_CATEGORIES.has(e.category));
    return own.length ? own[own.length - 1] : null;
  }, [payment]);

  const candidateIds = useMemo(() => {
    if (scenarioKey === "R5") return orphan ? [orphan.exceptionId] : [];
    if (scenarioKey === "R2") return [orphan?.exceptionId, ownException?.exceptionId].filter(Boolean);
    return ownException ? [ownException.exceptionId] : [];
  }, [scenarioKey, orphan, ownException]);

  const agents = useAgentRecords(candidateIds, tick);

  // verify() clears proposedAction; remember the last one so the pane can still show it.
  useEffect(() => {
    const fresh = Object.entries(agents).filter(([, a]) => a?.proposedAction);
    if (fresh.length) setLastProposals((prev) => ({ ...prev, ...Object.fromEntries(fresh.map(([id, a]) => [id, a.proposedAction])) }));
  }, [agents]);

  // R2: follow whichever twin carries the proposal; otherwise the first candidate.
  const followedId =
    ctx.followedId ||
    candidateIds.find((id) => agents[id]?.proposedAction || lastProposals[id]) ||
    candidateIds.find((id) => agents[id]?.verification) ||
    candidateIds[0] ||
    null;
  const agent = followedId ? agents[followedId] : null;

  const trackedExceptions = [ownException, needsOrphan ? orphan : null].filter(Boolean);
  const followedException = trackedExceptions.find((e) => e.exceptionId === followedId) || null;

  const reconciled =
    payment?.lifecycle?.reconciliationStatus === "RECONCILED" ||
    (followedException && followedException.status !== "OPEN" && agent?.verification?.result === "RESOLVED");
  const escalated =
    agent?.verification?.result === "ESCALATED" || (followedException?.awaitingCounterparty && followedException?.status === "OPEN");
  const outcome = reconciled ? "RECONCILED" : escalated ? "ESCALATED" : null;

  const countdownLeft = step.countdown && ctx.settledAt
    ? Math.max(0, Math.ceil(OVERDUE_SECONDS - (now - ctx.settledAt) / 1000))
    : 0;

  function waitingReason() {
    if (step.countdown && countdownLeft > 0) return `Statement window closes in ${countdownLeft}s`;
    if (step.gate === "proposal") {
      if (!followedId) return "Waiting for the exception to appear…";
      if (!agent?.proposedAction && !agent?.verification) return "Agent investigating…";
    }
    if (step.gate === "recheck") {
      if (!followedId) return "Waiting for the MISSING exception to appear…";
      if (!agent?.nextCheckAt && !agent?.recheckCount && !agent?.verification) return "Agent investigating…";
    }
    if (step.gate === "decided" && !ctx.decision) return "Approve or reject the proposal →";
    if (step.final && !outcome) {
      return scenarioKey === "TL" ? "Waiting for the agent's next sweep…" : "Waiting for the outcome…";
    }
    return null;
  }
  const waiting = waitingReason();

  async function next() {
    if (running || waiting || step.final) return;
    setRunning(true);
    setError(null);
    try {
      const { patch, note: n } = step.run ? await runAction(step.run, scenarioKey) : { patch: {}, note: null };
      setCtx((c) => ({ ...c, ...patch }));
      setNote(n);
      setIndex((i) => Math.min(i + 1, steps.length - 1));
      setTick((t) => t + 1);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setRunning(false);
    }
  }

  function onDecided(decision) {
    setCtx((c) => ({ ...c, decision, followedId }));
    setNote(decision === "APPROVE" ? "Approved — the agent executed and verified." : "Rejected — the run ended.");
    setIndex((i) => Math.min(i + 1, steps.length - 1));
    setTick((t) => t + 1);
  }

  return (
    <div className={styles.walkthrough}>
      <div className={styles.stepperBar}>
        <Stepper currentStep={index} maxDisplayedSteps={steps.length}>
          {steps.map((s) => (
            <Step key={s.key}>{s.label}</Step>
          ))}
        </Stepper>
      </div>

      <Card className={styles.narration}>
        <div className={styles.narrationText}>
          <div className={styles.narrationHead}>
            <Badge variant="blue">{scenario.key}</Badge>
            <H2 className={styles.narrationTitle}>
              {index + 1}. {step.label}
            </H2>
          </div>
          <Body className={styles.narrationBody}>{step.narration}</Body>
          {note && <Body className={styles.note}>✓ {note}</Body>}
          {error && <Body className={styles.error}>{error}</Body>}
          {step.final && outcome && (
            <div className={styles.outcome}>
              <Badge variant={outcome === "RECONCILED" ? "green" : "yellow"}>{outcome}</Badge>
              <Body>Expected: {scenario.expected}</Body>
            </div>
          )}
        </div>
        <div className={styles.narrationActions}>
          {!step.final && (
            <Button variant="primary" size="large" disabled={running || !!waiting} onClick={next}>
              {running ? "Running…" : step.gate ? "Continue →" : "Next →"}
            </Button>
          )}
          {waiting && <Body className={styles.muted}>{waiting}</Body>}
          <Button size="small" onClick={onReset}>
            Choose another scenario
          </Button>
        </div>
      </Card>

      <div className={styles.panes}>
        <PaymentTracker init={ctx.init} payment={payment} trace={trace} exceptions={trackedExceptions} />
        <AgentThinking
          exceptionId={followedId}
          agent={agent}
          lastProposal={followedId ? lastProposals[followedId] : null}
          policyLine={scenario.policyLine}
          canDecide={step.gate === "decided" && !ctx.decision}
          active={!outcome}
          onDecided={onDecided}
        />
      </div>
    </div>
  );
}
