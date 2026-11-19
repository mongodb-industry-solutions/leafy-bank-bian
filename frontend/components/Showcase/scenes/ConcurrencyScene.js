"use client";

// The W1 walkthrough's panels: the balance strip and one card per wire (evidence), the MongoDB
// setup beside the console (aside), and the verdict (result).

import { useEffect, useState } from "react";
import Link from "next/link";
import Badge from "@leafygreen-ui/badge";
import Code from "@leafygreen-ui/code";
import Icon from "@leafygreen-ui/icon";
import { Body, InlineCode, Overline } from "@leafygreen-ui/typography";
import { usd } from "../concurrencyRunLog";
import { groupsOf, stepCount } from "../concurrencyOps";
import styles from "../Showcase.module.css";

const STEP_MS = 800;

/** Available, hold and how many wires reserved. `payments` lets the balance follow later changes. */
export function BalanceStrip({ report }) {
  const available = report ? report.summary.finalAvailable : 200;
  const hold = report ? report.summary.finalHold : 0;
  return (
    <div className={styles.gapRow}>
      <div className={`${styles.gapSide} ${report ? (available < 0 ? styles.gapBad : styles.gapOk) : ""}`}>
        <Overline>Available</Overline>
        <span className={styles.gapFigure}>{usd(available)}</span>
      </div>
      <div className={styles.gapSide}>
        <Overline>On hold</Overline>
        <span className={styles.gapFigure}>{usd(hold)}</span>
      </div>
      <div className={styles.gapSide}>
        <Overline>Wires reserved</Overline>
        <span className={styles.gapFigure}>{report ? `${report.summary.winners} of ${report.params.workers}` : "0 of 3"}</span>
      </div>
    </div>
  );
}

/** One shared counter, so step n of every wire appears together: the race reads as concurrent. */
function useStepReveal(report, total) {
  const [shown, setShown] = useState(0);
  useEffect(() => {
    if (!report) return undefined;
    setShown(1);
    const id = setInterval(() => setShown((n) => {
      if (n >= total) clearInterval(id);
      return Math.min(n + 1, total);
    }), STEP_MS);
    return () => clearInterval(id);
  }, [report, total]);
  return shown;
}

const STEP_ICON = { ok: "Checkmark", wait: "Refresh", refuse: "XWithCircle", info: "MagnifyingGlass" };
const OUTCOME_BADGE = {
  RESERVED: ["green", "Reserved"],
  REFUSED_INSUFFICIENT_FUNDS: ["lightgray", "Refused"],
  ERROR: ["red", "Error"],
};

function StepRow({ step }) {
  return (
    <li className={`${styles.opsStep} ${styles[`ops_${step.tone}`]}`}>
      <div className={styles.opsRow}>
        <span className={styles.opsDot} aria-hidden="true"><Icon glyph={STEP_ICON[step.tone]} size={12} /></span>
        <Body as="span" className={styles.opsStepTitle}>{step.title}</Body>
        {step.result && <span className={styles.opsResult}>{step.result.replaceAll("_", " ").toLowerCase()}</span>}
      </div>
      <Body as="span" className={styles.opsNote}>{step.note}</Body>
      <details className={styles.opsCode} open={!!step.open}>
        <summary>Command</summary>
        <pre className={styles.opsPre}>{step.code}</pre>
      </details>
    </li>
  );
}

function PaymentOpsCard({ attempt, amount, shown, payment }) {
  const groups = groupsOf(attempt, amount);
  const total = stepCount(attempt, amount);
  const [variant, label] = OUTCOME_BADGE[attempt.outcome] || OUTCOME_BADGE.ERROR;
  // The read is step 1; every group step after it counts toward the shared reveal.
  let budget = shown - 1;
  return (
    <div className={styles.opsCard}>
      <div className={styles.opsHead}>
        <Body as="span" className={styles.opsTitle}>Wire {attempt.worker}</Body>
        {shown >= total && <Badge variant={variant}>{label}</Badge>}
        {shown >= total && attempt.writeConflicts > 0 && <Badge variant="yellow">{attempt.writeConflicts} retries</Badge>}
      </div>
      {payment?.lifecycle?.currentState && shown >= total && (
        <Body as="span" className={styles.opsNote}>
          {attempt.paymentId} · {payment.lifecycle.currentState.replaceAll("_", " ").toLowerCase()}
        </Body>
      )}
      <ol className={styles.opsList}>
        {shown >= 1 && (
          <StepRow step={{
            tone: "info", title: "Read the balance",
            note: `Sees ${usd(attempt.observedAvailable ?? 0)}. Every wire sees the same figure, and the guard does not rely on it.`,
            code: `db.accounts.find_one({"accountId": "${attempt.accountId || "ACC-w1demo"}"})`,
          }} />
        )}
        {groups.map((g) => {
          const visible = g.steps.slice(0, Math.max(budget, 0));
          budget -= g.steps.length;
          if (!visible.length) return null;
          return (
            <li key={g.label} className={styles.opsGroup}>
              <span className={styles.opsWhere}>{g.label}</span>
              <ol className={styles.opsList}>
                {visible.map((st, i) => <StepRow key={`${g.label}:${i}`} step={st} />)}
              </ol>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/** What each concurrent wire ran against MongoDB, step by step. */
export function PaymentOps({ report, payments }) {
  const total = Math.max(...report.attempts.map((a) => stepCount(a, report.params.amount)));
  const shown = useStepReveal(report, total);
  return (
    <div className={styles.opsGrid}>
      {report.attempts.map((a) => (
        <PaymentOpsCard key={a.worker} attempt={a} amount={report.params.amount} shown={shown} payment={payments[a.paymentId]} />
      ))}
    </div>
  );
}

const FALLBACK_GUARD = {
  filter: { accountId: "<accountId>", "balance.available": { $gte: "<amount>" } },
  update: { $inc: { "balance.available": "-<amount>", "balance.hold": "<amount>" } },
};

const pretty = (value) => JSON.stringify(value, null, 2);
const stripStamps = (update) => (update?.$inc ? { $inc: update.$inc } : update);

function Step({ n, title, chip, children }) {
  return (
    <li className={styles.setupStep}>
      <div className={styles.setupStepHead}>
        <span className={styles.setupNum}>{n}</span>
        <Body as="span" className={styles.setupStepTitle}>{title}</Body>
        <Badge variant="green" className={styles.setupChip}>{chip}</Badge>
      </div>
      <div className={styles.setupStepBody}>{children}</div>
    </li>
  );
}

/** The right-hand panel: what the platform does to prevent write skew, in lifecycle order. */
export function SetupPanel({ live }) {
  const guard = live?.guard || FALLBACK_GUARD;
  const reserve = `const filter = ${pretty(guard.filter)};\nconst update = ${pretty(stripStamps(guard.update))};`;
  return (
    <div className={styles.setupPanel}>
      <div className={styles.setupHead}>
        <Icon glyph="Database" size={16} className={styles.payFactIcon} />
        <Overline>How MongoDB prevents write skew</Overline>
        {live && <Badge variant="green">Live</Badge>}
      </div>
      <ol className={styles.setupSteps}>
        <Step n={1} title="One shared document" chip="Stage 3">
          <Body>Every wire writes the <strong>same</strong> <InlineCode>accounts</InlineCode> document, so MongoDB can see writers collide.</Body>
          <Code language="javascript" copyButtonAppearance="none" showLineNumbers={false}>
            {"{ balance: { current, available, hold } }"}
          </Code>
        </Step>
        <Step n={2} title="Reserve at stage 3" chip="Stage 3">
          <Body>One atomic <InlineCode>findOneAndUpdate</InlineCode>. The check and the decrement are the <strong>same</strong> operation.</Body>
          <Code language="javascript" copyButtonAppearance="none" showLineNumbers={false}>{reserve}</Code>
        </Step>
        <Step n={3} title="No match, payment rejected" chip="Stage 3">
          <Body>
            If the filter does not match, the payment is rejected for <strong>insufficient funds</strong>.
            Racing writers hit <InlineCode>WriteConflict</InlineCode> (112) and retry on fresh state.
          </Body>
        </Step>
        <Step n={4} title="Debit at stage 5" chip="Stage 5">
          <Body>One transaction. All four commit, or none do.</Body>
          <ul className={styles.setupChecks}>
            <li><Icon glyph="Checkmark" size={12} />Release the hold</li>
            <li><Icon glyph="Checkmark" size={12} />Debit with the same <InlineCode>$gte</InlineCode> guard</li>
            <li><Icon glyph="Checkmark" size={12} />Credit the receiver</li>
            <li><Icon glyph="Checkmark" size={12} />Insert the <InlineCode>transactions</InlineCode> record</li>
          </ul>
        </Step>
      </ol>
    </div>
  );
}

const STATE_LABEL = (p) => p?.lifecycle?.currentState?.replaceAll("_", " ").toLowerCase() || "unknown";

/** What the guard left in the balance, and what became of each wire. */
export function Verdict({ live, payments, reconciled }) {
  if (!live) return null;
  const s = live.summary;
  return (
    <div className={styles.resultBar}>
      <div className={styles.resultHead}>
        <Badge variant={s.invariantHeld ? "green" : "red"}>{s.invariantHeld ? "INVARIANT HELD" : "INVARIANT BROKEN"}</Badge>
        <span className={styles.resultGap}>
          {s.winners} reserved · {s.refused} refused
          {s.errors ? ` · ${s.errors} errors` : ""} · {s.totalWriteConflicts} write conflicts retried
        </span>
      </div>
      <div className={styles.gapRow}>
        <div className={`${styles.gapSide} ${s.invariantHeld ? styles.gapOk : styles.gapBad}`}>
          <Overline>Available after the race</Overline>
          <span className={styles.gapFigure}>{usd(s.finalAvailable)}</span>
          <Body as="span">{s.winners} of {live.params.workers} wires passed</Body>
        </div>
        {live.attempts.map((a) => (
          <div key={a.worker} className={styles.gapSide}>
            <Overline>Wire {a.worker}</Overline>
            <Body as="span" className={styles.mono}>{a.paymentId}</Body>
            <Body as="span">{a.outcome === "RESERVED" ? STATE_LABEL(payments[a.paymentId]) : "rejected, insufficient funds"}</Body>
            {a.outcome === "RESERVED" && (
              <Link href={`/payments-workflow?payment=${encodeURIComponent(a.paymentId)}`} className={styles.resultLink}>
                Open in Payments →
              </Link>
            )}
          </div>
        ))}
      </div>
      <Body className={styles.resultText}>
        {reconciled
          ? "Both winning wires were debited, settled, journaled and reconciled against the statement. The refused wire never left the bank."
          : "Waiting for the winning wires to reconcile."}
      </Body>
    </div>
  );
}
