"use client";

// W1: the write-skew walkthrough, built on the same console as the agent scenarios. Three real
// wires leave one persistent demo account at once and race through the payment saga. The stage 3
// reserve is a guarded update on one shared account document, so exactly two get through; those
// two are debited at stage 5, then settled, journaled and reconciled like any other wire.
// There is no agent: the database makes the decision.

import { useEffect, useMemo, useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import { Body } from "@leafygreen-ui/typography";
import { CONCURRENCY_STEPS, concurrencyByKey } from "./concurrencyScenarios";
import { bookStatement, getPayment, runLedgerBatch, runRace, settleDue } from "./concurrencyActions";
import { CONCURRENCY_FILTERS, deriveConcurrencyEvents, usd } from "./concurrencyRunLog";
import { usePacedEvents } from "./runLog";
import { deriveActivity } from "./activity";
import { BriefStrip } from "./ScenarioBrief";
import PaymentPipeline from "./PaymentPipeline";
import RunConsole from "./RunConsole";
import { BalanceStrip, PaymentOps, SetupPanel, Verdict } from "./scenes/ConcurrencyScene";
import styles from "./Showcase.module.css";

const TICK_MS = 3000;

const ACCOUNT_FACTS = [
  ["Coin", "Opening balance USD 200.00"],
  ["Note", "3 wires × USD 80.00, reserved at stage 3"],
  ["Lock", "Invariant: balance.available >= 0"],
];

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

function AccountCard({ queued, report }) {
  return (
    <div className={`${styles.railCard} ${styles.toneBorder} ${styles.payCard}`}>
      <Body as="p" className={styles.paneTitle}>USD 200.00 · demo account ACC-w1demo</Body>
      {ACCOUNT_FACTS.map(([glyph, text]) => (
        <div key={text} className={styles.payFact}>
          <Icon glyph={glyph} size={14} className={styles.payFactIcon} />
          <Body as="span">{text}</Body>
        </div>
      ))}
      <div className={styles.chipRow}>
        <Badge variant={queued ? "blue" : "lightgray"}>{queued ? "3 wires queued" : "Not queued yet"}</Badge>
        {report && (
          <Badge variant={report.summary.invariantHeld ? "green" : "red"}>
            Available {usd(report.summary.finalAvailable)}
          </Badge>
        )}
      </div>
    </div>
  );
}

function ConcurrencyRun({ scenarioKey, onRestart }) {
  const scenario = concurrencyByKey(scenarioKey);
  const [index, setIndex] = useState(0);
  const [queued, setQueued] = useState(false);
  const [live, setLive] = useState(null);
  const [statement, setStatement] = useState(null);
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
  const { events, backlog } = usePacedEvents(allEvents);

  async function call(fn) {
    const { data, error: err } = await fn();
    if (err) throw new Error(String(err));
    return data;
  }

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

  const tone = reconciled ? "resolved" : live && !live.summary.invariantHeld ? "alert" : live ? "waiting" : "neutral";
  const activity = deriveActivity({ running: running ? RUNNING_TEXT[step.key] : null });
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
              <AccountCard queued={queued} report={live} />
            </>
          )}
          pipeline={<PaymentPipeline payment={lead} status={status} paymentId={winners[0]?.paymentId} mode="reconciliation" reconciled={reconciled} />}
          controller={controller}
          filters={CONCURRENCY_FILTERS}
          events={events}
          pending={backlog > 0 ? "Next event coming…" : null}
          activity={activity}
          evidence={queued && (
            <div className={styles.section}>
              <BalanceStrip report={live} />
              {live && <PaymentOps report={live} payments={payments} />}
            </div>
          )}
          aside={<SetupPanel live={live} />}
          result={step.final && <div className={styles.section}><Verdict live={live} payments={payments} reconciled={reconciled} /></div>}
        />
      </div>
    </div>
  );
}

export default function ConcurrencyStepper({ scenarioKey }) {
  // Remounting resets every piece of state, including the paced log.
  const [run, setRun] = useState(0);
  return <ConcurrencyRun key={run} scenarioKey={scenarioKey} onRestart={() => setRun((n) => n + 1)} />;
}
