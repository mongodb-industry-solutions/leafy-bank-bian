"use client";

// Plan E — the step machine. One owner for the scenario's polls: a single 3s tick re-reads
// the payment, its trace, the OPEN exceptions list and the followed exceptions' agent{}.
// AgentThinking owns only its own steps poll.

import { useEffect, useMemo, useRef, useState } from "react";
import { Body, Overline } from "@leafygreen-ui/typography";
import { agentApi, coreApi, pipelineApi } from "@/lib/api/client";
import { usePaymentWorkflow, usePipelineTrace, useWorkflowExceptions } from "@/lib/api/hooks";
import { CATEGORY, OVERDUE_SECONDS, actOf, scenarioByKey, stepsFor } from "./scenarios";
import { PaymentCard } from "./StatusRail";
import { BriefStrip } from "./ScenarioBrief";
import PaymentPipeline from "./PaymentPipeline";
import RunConsole from "./RunConsole";
import AgentPanel from "./AgentPanel";
import { deriveActivity } from "./activity";
import { useAgentSteps } from "./AgentThinking";
import ProblemScene from "./scenes/ProblemScene";
import DecisionScene from "./scenes/DecisionScene";
import ResultScene from "./scenes/ResultScene";
import AgentFailure from "./scenes/AgentFailure";
import { gapOf, railLegOf } from "./scenes/format";
import { BEAT_DWELL_MS, phaseOfStep, usePacedEvents, useRunLog } from "./runLog";
import styles from "./Showcase.module.css";

const TICK_MS = 3000;
// Endings each scenario is designed to reach. ESCALATED is a legitimate waypoint for R4/R5
// (the correspondent has not answered yet); anywhere else it means the run went off script.
const EXPECTED_OUTCOMES = {
  R1: ["RECONCILED"], R1b: ["RECONCILED"], R2: ["RECONCILED"], TL: ["RECONCILED"],
  R4: ["RECONCILED", "ESCALATED"], R5: ["ANSWERED", "ESCALATED"],
};
const BATCH_KICK_GAP_MS = 12000;
const BATCH_KICK_LIMIT = 5;
// An automatic step fires this long after the log has finished showing the step before it,
// so each beat is read before the next one starts.
const AUTO_DELAY_MS = 2000;
// Gap between log lines. Key beats hold longer through their own `dwell` (see runLog).
const LOG_GAP_MS = 1500;
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
      received: { id: data.paymentMessageId, lines: data.entryCount, orphans: data.orphanCount || 0, late: kind === "statementLate" },
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

  const { steps: agentSteps, started: agentStarted } = useAgentSteps(followedId, !outcome);

  // The payment flips to RECONCILED on the next GL batch, which the scheduler runs every 10
  // minutes. Once the agent has closed the exception, trigger the batch ourselves (the earlier
  // steps do the same) and retry a few times: the adjustment journal may not be posted yet.
  const batchKicks = useRef({ at: 0, count: 0 });
  const agentResolved = agent?.verification?.result === "RESOLVED";
  const awaitingBatch = scenarioKey !== "R5" && agentResolved && !reconciled;
  useEffect(() => {
    const k = batchKicks.current;
    if (!awaitingBatch || k.count >= BATCH_KICK_LIMIT || Date.now() - k.at < BATCH_KICK_GAP_MS) return;
    k.at = Date.now();
    k.count += 1;
    pipelineApi("batch/trigger", null, { method: "POST" });
  }, [awaitingBatch, tick]);

  // The first non-zero gap seen is the "before" of the result card; reconciling zeroes the live one.
  const railLeg = railLegOf(trace);
  const liveGap = gapOf(railLeg);
  const [gapBefore, setGapBefore] = useState(null);
  useEffect(() => {
    if (liveGap) setGapBefore((prev) => prev ?? liveGap);
  }, [liveGap]);

  // verify() clears proposedAction on the agent record; the remembered one keeps the proposal on screen.
  const proposal = agent?.proposedAction || (followedId ? lastProposals[followedId] : null);
  const { events: allEvents, push: pushEvent } = useRunLog({
    scenario, scenarioKey, steps, index, step, ctx, payment, trace, agentSteps, agentStarted, agent, proposal,
    exceptions: trackedExceptions, gapMajor: liveGap == null ? null : liveGap / 100,
  });
  // Lines appear one at a time, as in the cut-off walkthrough, so the presenter can read each.
  const { events, backlog } = usePacedEvents(allEvents, LOG_GAP_MS);

  // The lifecycle strip follows what the log has shown, not the live payment, which is seconds
  // ahead: it ticks to stage N when the "Stage N" line is read, is held once the exception line
  // has been read, and completes when "Payment reconciled" is read.
  const pacedStrip = useMemo(() => {
    const keys = events.map((e) => e.key);
    // `exc:<id>` is an exception opening; `exc:<id>:<STATUS>` is it closing. Held while any shown one is still open.
    const closed = new Set(keys.filter((k) => /^exc:.+:[A-Z]+$/.test(k)).map((k) => k.slice(0, k.lastIndexOf(":"))));
    const opened = keys.filter((k) => k.startsWith("exc:") && !/^exc:.+:[A-Z]+$/.test(k));
    return {
      ceiling: Math.max(0, ...keys.filter((k) => /^stage:\d$/.test(k)).map((k) => Number(k.slice(6)))),
      held: opened.some((k) => !closed.has(k)),
      complete: keys.includes("reconciled"),
    };
  }, [events]);

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
      if (awaitingBatch) return "Exception resolved. Waiting for the ledger batch to mark the payment reconciled…";
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

  // Steps marked `auto` run on their own once reached, but only after the paced log has caught
  // up (backlog 0), so a step never starts while the previous one is still being narrated.
  // Each tries once; a failure leaves the button for a manual retry instead of looping.
  const nextRef = useRef(next);
  nextRef.current = next;
  const autoTried = useRef(new Set());
  useEffect(() => {
    if (!step.auto || running || waiting || backlog > 0 || autoTried.current.has(step.key)) return undefined;
    const id = setTimeout(() => {
      autoTried.current.add(step.key);
      nextRef.current();
    }, AUTO_DELAY_MS);
    return () => clearTimeout(id);
  }, [step.key, step.auto, running, waiting, backlog]);

  async function next() {
    if (running || waiting || step.final) return;
    setRunning(true);
    setError(null);
    // A step may declare `chain` to run the ones after it in one click (none do now). Each success
    // advances the index, so a failure part-way leaves the failed step current and Next retries.
    const last = Math.min(index + (step.chain || 0), steps.length - 1);
    let local = ctx;
    let at = index;
    try {
      for (; at <= last; at += 1) {
        const run = steps[at].run;
        const { patch, note: n, received } = run ? await runAction(run, scenarioKey, local.paymentId) : { patch: {}, note: null };
        local = { ...local, ...patch };
        setCtx(local);
        setNote(n);
        if (received) {
          // Its own line, ahead of the step's MongoDB writes, so the arrival reads as an event.
          pushEvent({
            key: `statement-received:${received.id}`,
            phase: phaseOfStep(steps[at].key),
            lane: "code",
            ord: at - 0.5,
            tone: "good",
            dwell: BEAT_DWELL_MS,
            title: `${received.late ? "Late statement" : "Statement"} received from ${scenario.bank}`,
            detail: `${received.id}: ${received.lines} line${received.lines === 1 ? "" : "s"}${received.orphans ? `, ${received.orphans} not ours` : ""}`,
          });
        } else if (n) pushEvent({ key: `note:${steps[at].key}`, phase: phaseOfStep(steps[at].key), lane: "code", title: n, stepKey: steps[at].key });
        setIndex(Math.min(at + 1, steps.length - 1));
      }
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
    // Deciding early skips the approve step, which has no button of its own.
    setIndex(steps.length - 1);
    setTick((t) => t + 1);
  }

  const anyOpen = trackedExceptions.some((e) => e.status === "OPEN");
  // One tone for the whole page, so the scene stripe and status card agree.
  const tone =
    outcome === "RECONCILED" || outcome === "ANSWERED" ? "resolved"
    : awaitingReply || outcome === "ESCALATED" ? "waiting"
    : anyOpen ? "alert"
    : "neutral";
  // The proposal is decidable as soon as the agent has made one; waiting for the next step's
  // button would leave it hidden behind "Review the proposal".
  const proposalReady = step.gate === "proposal" && !!agent?.proposedAction && !ctx.decision;
  const canDecide = (step.gate === "decided" && !ctx.decision) || proposalReady || revisedProposalPending;
  const sources = { payment, trace, ownException, orphan, agent, followedException, scenarioKey };

  // The decision opens only once the paced log has shown "Run paused for approval", i.e. after the
  // presenter has read the proposal and policy lines. `canDecide` itself stays live (it gates the
  // buttons and the wait text); this only delays when the bar appears.
  const decisionShown = !!proposal && (canDecide || revisedProposalPending) && events.some((e) => e.key === "paused");

  const agentGate = step.gate === "proposal" || step.gate === "recheck";
  const agentWorking =
    (step.gate === "proposal" && !agent?.proposedAction && !agent?.verification) ||
    (step.gate === "recheck" && !agent?.nextCheckAt && !agent?.recheckCount && !agent?.verification) ||
    step.final;
  const activity = deriveActivity({
    humanWait: canDecide ? "Waiting for your decision" : null,
    running: running ? step.auto || "Applying this step" : null,
    externalWait:
      step.countdown && countdownLeft > 0 ? `Statement window closes in ${countdownLeft}s`
      : awaitingReply ? "Waiting for the correspondent bank's reply"
      : awaitingBatch ? "Waiting for the ledger batch to reconcile the payment"
      : step.final && !outcome && scenarioKey === "TL" ? "Waiting for the agent's next sweep"
      : null,
    systemWait: agentGate && !followedId ? "Waiting for the exception to appear" : null,
    agentBusy: !!followedId && !outcome && !agent?.error && agentWorking && !revisedProposalPending && !awaitingBatch,
    agentSteps,
    bank: scenario.bank,
  });

  const controller = {
    title: step.final ? (outcome ? "Run complete" : "Verifying the result") : step.title,
    label: step.button || "Next",
    final: !!step.final,
    // An automatic step shows its button only after it failed, so it can be retried.
    showButton: !step.final && step.key !== "approve" && !(step.auto && !error),
    running,
    waiting,
    awaitingReply,
    error,
    onNext: next,
    onReplyNow: replyNow,
    onReset,
  };

  return (
    <div className={`${styles.walkthrough} ${styles[`tone_${tone}`]}`}>
      <div className={styles.stageFull}>
        <RunConsole
          stepIndex={index}
          stepCards={steps.map((s) => ({
            key: s.key,
            title: s.final && !outcome ? "Verifying the result" : s.final ? "Run complete" : s.title,
            summary: s.summary,
            beat: scenario.beats?.[s.key],
          }))}
          top={(
            <>
              <BriefStrip scenario={scenario} />
              <PaymentCard scenario={scenario} init={ctx.init} payment={payment} exceptions={trackedExceptions} />
            </>
          )}
          pipeline={(
            <PaymentPipeline
              mode="reconciliation"
              payment={payment}
              status={ctx.init?.status}
              paymentId={ctx.init?.paymentId}
              reconciled={payment?.lifecycle?.reconciliationStatus === "RECONCILED"}
              exceptionsOpen={anyOpen}
              paced={pacedStrip}
            />
          )}
          controller={controller}
          activity={activity}
          events={events}
          pending={backlog > 0 ? "Next event coming…" : null}
          sources={sources}
          evidence={actOf(step.key) >= 2 && (
            <ProblemScene
              scenarioKey={scenarioKey}
              paymentId={paymentId}
              payment={payment}
              trace={trace}
              exceptions={trackedExceptions}
              bank={scenario.bank}
              countdown={!!step.countdown}
              countdownLeft={countdownLeft}
            />
          )}
          failure={agent?.error && !agent?.verification && (
            <AgentFailure error={agent.error} exceptionId={followedId} paymentId={followedException?.paymentId} />
          )}
          aside={(
            <AgentPanel
              agent={agent}
              steps={agentSteps}
              started={agentStarted}
              activity={activity}
              activation={followedId ? { text: "Woken by an insert on exceptions (change stream)", at: agent?.startedAt } : null}
              bank={scenario.bank}
              needsDecision={decisionShown}
            />
          )}
          decision={decisionShown && (
            <DecisionScene
              compact
              scenario={scenario}
              exceptionId={followedId}
              proposal={proposal}
              canDecide={canDecide}
              onDecided={onDecided}
            />
          )}
          result={step.final && !revisedProposalPending && (
            <ResultScene
              paymentId={ctx.paymentId}
              scenario={scenario}
              trace={trace}
              payment={payment}
              outcome={outcome}
              gapBefore={gapBefore}
              expectedOutcomes={EXPECTED_OUTCOMES[scenarioKey] || []}
              waiting={waiting}
            />
          )}
        />
      </div>
    </div>
  );
}
