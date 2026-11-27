"use client";

// W1: the write-skew walkthrough, built on the same console as the agent scenarios. Three real
// wires leave one persistent demo account at once and race through the payment saga. The stage 3
// reserve is a guarded update on one shared account document, so exactly two get through; those
// two are debited at stage 5, then settled, journaled and reconciled like any other wire.
// There is no agent: the database makes the decision.

import { useEffect, useMemo, useRef, useState } from "react";
import Button from "@leafygreen-ui/button";
import { CONCURRENCY_STEPS, concurrencyByKey } from "./concurrencyScenarios";
import { bookStatement, getPayment, runLedgerBatch, runRace, settleDue } from "./concurrencyActions";
import { CONCURRENCY_FILTERS, deriveConcurrencyEvents } from "./concurrencyRunLog";
import { usePacedEvents } from "./runLog";
import { clearSavedRun, hasSavedRun, useSavedState } from "./savedRun";
import { deriveActivity } from "./activity";
import PaymentPipeline from "./PaymentPipeline";
import RunConsole from "./RunConsole";
import { BriefStrip } from "./ScenarioBrief";
import { BalanceGauge, DesignPanel, PaymentOps, RaceCard, SkewContrast, Verdict } from "./scenes/ConcurrencyScene";
import styles from "./Showcase.module.css";

const TICK_MS = 3000;
const AUTO_DELAY_MS = 2000;

// What the database must show before an auto step may start. The log's pace is not enough:
// settlement and posting land asynchronously, and booking a statement before any wire is
// SETTLED fails with "No statement generated".
const isSettled = (p) => ["SETTLED", "COMPLETED"].includes(p?.status);
const isPosted = (p) => p?.lifecycle?.postingStatus === "POSTED";
const READY = {
  glpost: (wins) => wins.every(isSettled),
  statement: (wins) => wins.every((p) => isSettled(p) && isPosted(p)),
};

const WAITING_TEXT = {
  glpost: "Waiting for both wires to settle",
  statement: "Waiting for both wires to post to the ledger",
};

const RUNNING_TEXT = {
  race: "Sending three wires through the saga",
  settle: "Settling the wires",
  glpost: "Posting to the ledger",
  statement: "Receiving the statement",
  match: "Reconciling",
};

/** Every raced payment, re-read on the shared tick, keyed by paymentId. */
function usePayments(ids, tick) {
  const [payments, setPayments] = useState({});
  const key = ids.join(",");
  useEffect(() => {
    if (!key) return undefined;
    let cancelled = false;
    Promise.all(key.split(",").map((id) => getPayment(id).then(({ data }) => [id, data]))).then((pairs) => {
      if (!cancelled) setPayments((prev) => ({ ...prev, ...Object.fromEntries(pairs.filter(([, d]) => d)) }));
    });
    return () => {
      cancelled = true;
    };
  }, [key, tick]);
  return payments;
}

function ConcurrencyRun({ scenarioKey, onRestart }) {
  const scenario = concurrencyByKey(scenarioKey);
  const [restored] = useState(() => hasSavedRun(scenarioKey));
  const [index, setIndex] = useSavedState(scenarioKey, "index", 0);
  const [queued, setQueued] = useSavedState(scenarioKey, "queued", false);
  const [live, setLive] = useSavedState(scenarioKey, "live", null);
  const [statement, setStatement] = useSavedState(scenarioKey, "statement", null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), TICK_MS);
    return () => clearInterval(id);
  }, []);

  const step = CONCURRENCY_STEPS[index];
  const ids = useMemo(() => (live ? live.attempts.map((a) => a.paymentId).filter(Boolean) : []), [live]);
  const payments = usePayments(ids, tick);
  const winners = live ? live.attempts.filter((a) => a.outcome === "RESERVED") : [];
  const reconciled = winners.length > 0 && winners.every((a) => payments[a.paymentId]?.lifecycle?.reconciliationStatus === "RECONCILED");

  const allEvents = useMemo(
    () => deriveConcurrencyEvents({ queued, live, payments, statement }),
    [queued, live, payments, statement]
  );
  const { events, backlog } = usePacedEvents(allEvents, undefined, { catchUp: restored });

  async function call(fn) {
    const { data, error: err } = await fn();
    if (err) throw new Error(String(err));
    return data;
  }

  const winningPayments = winners.map((a) => payments[a.paymentId]);
  const ready = !READY[step.key] || (winningPayments.length > 0 && READY[step.key](winningPayments));

  // Same rule as the reconciliation walkthrough: an `auto` step starts once the paced log has caught
  // up, so it never runs while the previous one is still being narrated. Each tries once; a failure
  // leaves the button for a manual retry instead of looping.
  const runRef = useRef(null);
  const autoTried = useRef(new Set());
  useEffect(() => {
    if (!step.auto || running || !ready || backlog > 0 || autoTried.current.has(step.key)) return undefined;
    const id = setTimeout(() => {
      autoTried.current.add(step.key);
      runRef.current();
    }, AUTO_DELAY_MS);
    return () => clearTimeout(id);
  }, [step.key, step.auto, running, ready, backlog]);

  async function runStep() {
    setRunning(true);
    setError(null);
    try {
      if (step.key === "queue") setQueued(true);
      else if (step.key === "race") setLive(await call(runRace));
      else if (step.key === "settle") await call(settleDue);
      else if (step.key === "glpost" || step.key === "match") await call(runLedgerBatch);
      else if (step.key === "statement") {
        const data = await call(bookStatement);
        if (!data?.generated) throw new Error("No statement generated: no settled wire is waiting to be booked.");
        setStatement({ id: data.paymentMessageId, lines: data.entryCount });
      }
      setIndex((i) => Math.min(i + 1, CONCURRENCY_STEPS.length - 1));
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setRunning(false);
      setTick((t) => t + 1);
    }
  }

  runRef.current = runStep;

  const tone = reconciled ? "resolved" : live && !live.summary.invariantHeld ? "alert" : live ? "waiting" : "neutral";
  const activity = deriveActivity({
    running: running ? RUNNING_TEXT[step.key] : null,
    systemWait: !running && step.auto && !ready ? WAITING_TEXT[step.key] : null,
  });
  const lead = winners[0] ? payments[winners[0].paymentId] : null;
  const status = lead?.lifecycle?.currentState || (queued ? "VALIDATED" : null);

  const controller = {
    label: step.button,
    final: !!step.final,
    showButton: !step.final,
    running,
    waiting: null,
    error,
    onNext: runStep,
    extra: step.final && (
      reconciled
        ? <Button size="small" onClick={onRestart}>Race again</Button>
        : <Button size="small" disabled={running} onClick={() => call(runLedgerBatch).then(() => setTick((t) => t + 1)).catch((e) => setError(e.message))}>Run batch again</Button>
    ),
  };

  return (
    <div className={`${styles.walkthrough} ${styles[`tone_${tone}`]}`}>
      <div className={styles.stageFull}>
        <RunConsole
          stepIndex={index}
          stepCards={CONCURRENCY_STEPS.map((s) => ({
            key: s.key,
            title: s.final ? (reconciled ? "Run complete" : "Verifying the result") : s.title,
            summary: s.summary,
            beat: s.beat,
          }))}
          top={(
            <>
              <BriefStrip scenario={scenario} />
              <RaceCard scenario={scenario} report={live} queued={queued} />
            </>
          )}
          pipeline={<PaymentPipeline payment={lead} status={status} paymentId={winners[0]?.paymentId} mode="reconciliation" reconciled={reconciled} />}
          controller={controller}
          filters={CONCURRENCY_FILTERS}
          events={events}
          pending={backlog > 0 ? "Next event coming…" : null}
          activity={activity}
          evidence={(
            <div className={styles.section}>
              <BalanceGauge report={live} />
              <SkewContrast report={live} />
              {live && (
                // Engineer detail: the result above tells the story; this shows every command.
                <details className={styles.w1Detail}>
                  <summary>See what each wire ran on MongoDB</summary>
                  <PaymentOps report={live} payments={payments} />
                </details>
              )}
            </div>
          )}
          aside={<DesignPanel live={live} stepKey={step.key} />}
          result={step.final && <div className={styles.section}><Verdict live={live} payments={payments} reconciled={reconciled} /></div>}
        />
      </div>
    </div>
  );
}

export default function ConcurrencyStepper({ scenarioKey }) {
  // Remounting resets every piece of state, including the paced log; clearing the saved run first
  // stops the remount from restoring it.
  const [run, setRun] = useState(0);
  const restart = () => {
    clearSavedRun(scenarioKey);
    setRun((n) => n + 1);
  };
  return <ConcurrencyRun key={run} scenarioKey={scenarioKey} onRestart={restart} />;
}
