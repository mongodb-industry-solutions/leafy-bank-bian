"use client";

// Plan E — the step machine. One owner for the scenario's polls: a single 3s tick re-reads
// the payment, its trace, the OPEN exceptions list and the followed exceptions' agent{}.
// AgentThinking owns only its own steps poll.

import { useEffect, useMemo, useState } from "react";
import Button from "@leafygreen-ui/button";
import Badge from "@leafygreen-ui/badge";
import Card from "@leafygreen-ui/card";
import Stepper, { Step } from "@leafygreen-ui/stepper";
import { H2, Body, Overline } from "@leafygreen-ui/typography";
import { agentApi, coreApi, pipelineApi } from "@/lib/api/client";
import { usePaymentWorkflow, usePipelineTrace, useWorkflowExceptions } from "@/lib/api/hooks";
import { CATEGORY, OVERDUE_SECONDS, scenarioByKey, stepsFor } from "./scenarios";
import PaymentTracker from "./PaymentTracker";
import AgentThinking from "./AgentThinking";
import StepDocuments from "./StepDocuments";
import styles from "./Showcase.module.css";

const TICK_MS = 3000;
// Endings each scenario is designed to reach. ESCALATED is a legitimate waypoint for R4/R5
// (the correspondent has not answered yet); anywhere else it means the run went off script.
const EXPECTED_OUTCOMES = {
  R1: ["RECONCILED"], R1b: ["RECONCILED"], R2: ["RECONCILED"], TL: ["RECONCILED"],
  R4: ["RECONCILED", "ESCALATED"], R5: ["ANSWERED", "ESCALATED"],
};
const OWN_CATEGORIES = new Set([CATEGORY.DISCREPANCY, CATEGORY.MISSING]);

const generateStatement = (includeOrphan) =>
  coreApi("FinancialGateway/GW-WIRE-01/Statement/Generate", {
    method: "POST",
    body: { accountCode: "1111", includeOrphan },
  });

/** Runs one step's backend action. Returns { patch, note } or throws with the error text. */
async function runAction(kind, scenarioKey, paymentId) {
  if (kind === "initiate") {
    const { data, error } = await coreApi("workflow/demo/recon-scenario", {
      method: "POST",
      body: { scenario: scenarioKey },
    });
    if (error) throw new Error(error);
    return {
      patch: { init: data, paymentId: data.paymentId },
      note: `Payment ${data.paymentId} initiated (${data.status}).`,
    };
  }
  if (kind === "settle") {
    const { data, error } = await coreApi("workflow/demo/settle-due", { method: "POST" });
    if (error) throw new Error(error);
    return {
      patch: { settledAt: Date.now() },
      note: data?.settled ? "Settlement confirmed." : "Already settled by the settlement worker.",
    };
  }
  if (kind === "batch") {
    const { error } = await pipelineApi("batch/trigger", null, { method: "POST" });
    if (error) throw new Error(error);
    return { patch: {}, note: "GL batch complete." };
  }
  if (kind === "statement" || kind === "statementLate") {
    const { data, error } = await generateStatement(scenarioKey === "R5");
    if (error) throw new Error(error);
    if (!data?.generated) {
      // R5 needs its injected orphan line, which only a fresh statement carries.
      if (scenarioKey === "R5") {
        throw new Error("No statement generated: this wire is already on a statement. Start the scenario again.");
      }
      return { patch: {}, note: "This wire is already on a statement." };
    }
    const orphans = data.orphanCount ? ` (${data.orphanCount} unknown)` : "";
    // The agent scheduled its recheck minutes ahead; the new statement is the reason to look now.
    if (kind === "statementLate" && paymentId) {
      await agentApi("reconciliation/wake", null, { method: "POST", body: { paymentId } });
    }
    const patch = kind === "statementLate" ? {} : { statementId: data.paymentMessageId };
    return {
      patch,
      note: `Statement ${data.paymentMessageId} received: ${data.entryCount} line${data.entryCount === 1 ? "" : "s"}${orphans}.`,
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
    // Only R5 has no payment. Elsewhere the payment's own reconciliation must close:
    // resolving one exception (e.g. dismissing an orphan twin) does not reconcile it.
    (scenarioKey === "R5" && followedException && followedException.status !== "OPEN" && agent?.verification?.result === "RESOLVED");
  const escalated =
    agent?.verification?.result === "ESCALATED" || (followedException?.awaitingCounterparty && followedException?.status === "OPEN");
  const awaitingReply = !!followedException?.awaitingCounterparty && followedException.status === "OPEN";
  // The correspondent's reply closes an escalated exception that the agent last saw as ESCALATED.
  const answered = !!followedException?.escalation?.reply && followedException.status !== "OPEN";
  const outcome = reconciled ? "RECONCILED" : answered ? "ANSWERED" : escalated ? "ESCALATED" : null;

  const countdownLeft = step.countdown && ctx.settledAt
    ? Math.max(0, Math.ceil(OVERDUE_SECONDS - (now - ctx.settledAt) / 1000))
    : 0;

  function waitingReason() {
    if (step.countdown && countdownLeft > 0) return `Statement window closes in ${countdownLeft}s`;
    if (agent?.error && !agent?.verification) return "Agent error — retry the investigation";
    if (step.gate === "proposal") {
      if (!followedId) return "Waiting for the exception…";
      if (!agent?.proposedAction && !agent?.verification) return "Agent investigating…";
    }
    if (step.gate === "recheck") {
      if (!followedId) return "Waiting for the missing-line exception…";
      if (!agent?.nextCheckAt && !agent?.recheckCount && !agent?.verification) return "Agent investigating…";
    }
    if (step.gate === "decided" && !ctx.decision) return "Approve or reject the proposal";
    if (step.final && !outcome) {
      if (revisedProposalPending) return "The agent revised its proposal. Approve or reject it";
      return scenarioKey === "TL" ? "Waiting for the agent's next sweep…" : "Waiting for the outcome…";
    }
    return null;
  }
  // A failed first action sends the agent back to re-investigate; its new proposal needs its own approval.
  const revisedProposalPending = ctx.decision === "APPROVE" && !!agent?.proposedAction;
  const waiting = waitingReason();

  async function replyNow() {
    setRunning(true);
    setError(null);
    try {
      const { error: err } = await pipelineApi(
        `exceptions/${followedId}/correspondent-reply`, null, { method: "POST" });
      if (err) throw new Error(err);
      setNote("The correspondent answered the investigation request.");
      setTick((t) => t + 1);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setRunning(false);
    }
  }

  async function next() {
    if (running || waiting || step.final) return;
    setRunning(true);
    setError(null);
    try {
      const { patch, note: n } = step.run ? await runAction(step.run, scenarioKey, ctx.paymentId) : { patch: {}, note: null };
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
    setNote(decision === "APPROVE" ? "Approved. The agent executed the action and verified it." : "Rejected. The run has ended.");
    setIndex((i) => Math.min(i + 1, steps.length - 1));
    setTick((t) => t + 1);
  }

  return (
    <div className={styles.walkthrough}>
      <div>
        <Stepper currentStep={step.final && outcome ? steps.length : index} maxDisplayedSteps={steps.length}>
          {steps.map((s) => (
            <Step key={s.key}>{s.label}</Step>
          ))}
        </Stepper>
      </div>

      <Card className={styles.narration}>
        <div className={styles.narrationText}>
          <div className={styles.narrationHead}>
            <H2 className={styles.narrationTitle}>
              {index + 1}. {step.title}
            </H2>
          </div>
          <Body>{step.narration}</Body>
          {scenario.beats?.[step.key] && (
            <div className={styles.beat}>
              <Overline>This scenario</Overline>
              <Body>{scenario.beats[step.key]}</Body>
            </div>
          )}
          {note && <Body className={styles.note}>{note}</Body>}
          {error && <Body className={styles.error}>{error}</Body>}
          {step.final && outcome && (
            <div className={styles.outcome}>
              <Badge variant={outcome === "ESCALATED" ? "yellow" : "green"}>
                {outcome === "ANSWERED" ? "CORRESPONDENT REPLIED" : outcome}
              </Badge>
              <Body>Expected: {scenario.expected}</Body>
              {!(EXPECTED_OUTCOMES[scenarioKey] || []).includes(outcome) && (
                <Body className={styles.error}>
                  This run ended differently than the scenario expects. Check the exception
                  and the payment before continuing.
                </Body>
              )}
            </div>
          )}
        </div>
        <div className={styles.narrationActions}>
          {!step.final && (
            <Button variant="primary" size="large" disabled={running || !!waiting} onClick={next}>
              {running ? "Running…" : "Next"}
            </Button>
          )}
          {waiting && <Body className={styles.muted}>{waiting}</Body>}
          {awaitingReply && (
            <Button size="small" variant="default" disabled={running} onClick={replyNow}>
              Correspondent replies now
            </Button>
          )}
          <Button size="small" variant="default" onClick={onReset}>
            All scenarios
          </Button>
        </div>
      </Card>

      <StepDocuments
        steps={steps}
        index={index}
        scenarioKey={scenarioKey}
        sources={{ payment, trace, ownException, orphan, agent, followedException, scenarioKey }}
      />

      <div className={styles.panes}>
        <PaymentTracker init={ctx.init} payment={payment} trace={trace} exceptions={trackedExceptions} />
        <AgentThinking
          exceptionId={followedId}
          paymentId={followedException?.paymentId}
          agent={agent}
          lastProposal={followedId ? lastProposals[followedId] : null}
          policyLine={scenario.policyLine}
          canDecide={(step.gate === "decided" && !ctx.decision) || revisedProposalPending}
          active={!outcome}
          onDecided={onDecided}
        />
      </div>
    </div>
  );
}
