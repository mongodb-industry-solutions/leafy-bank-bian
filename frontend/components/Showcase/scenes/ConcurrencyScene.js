"use client";

// The W1 walkthrough's panels: the balance strip and one card per wire (evidence), the MongoDB
// setup beside the console (aside), and the verdict (result).

import { useEffect, useState } from "react";
import Link from "next/link";
import Badge from "@leafygreen-ui/badge";
import Icon from "@leafygreen-ui/icon";
import { Body, InlineCode, Overline } from "@leafygreen-ui/typography";
import { usd } from "../concurrencyRunLog";
import { groupsOf, stepCount } from "../concurrencyOps";
import styles from "../Showcase.module.css";

const STEP_MS = 800;

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
            code: `db.accounts.find_one({"accountId": "${attempt.accountId || "ACC-acc10004"}"})`,
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

// Light JSON/code colouring (keys, strings, numbers, operators), as in the lending demo, so code
// wraps inside narrow panels instead of scrolling sideways.
const TOKEN = /("(?:[^"\\]|\\.)*"(?=\s*:))|("(?:[^"\\]|\\.)*")|(\$[a-zA-Z]+)|(-?\b\d+(?:\.\d+)?\b)|(#.*$)/gm;
function Tokens({ text }) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(TOKEN)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const cls = m[1] ? styles.tkKey : m[2] ? (m[2].startsWith('"$') ? styles.tkOp : styles.tkStr) : m[3] ? styles.tkOp : m[4] ? styles.tkNum : styles.tkComment;
    out.push(<span key={m.index} className={cls}>{m[0]}</span>);
    last = m.index + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}

function CodeBlock({ label, children }) {
  return (
    <div className={styles.wsCode}>
      {label && <span className={styles.wsLabel}>{label}</span>}
      <pre><Tokens text={children} /></pre>
    </div>
  );
}

function Step({ title, chip, active, done, children }) {
  return (
    <li className={`${styles.wsTlItem} ${active ? styles.wsTlActive : done ? "" : styles.wsTlIdle}`} aria-current={active ? "step" : undefined}>
      <span className={styles.wsTlDot} aria-hidden="true" />
      <div className={styles.wsTlHead}>
        <span className={styles.wsTlTitle}>{title}</span>
        <span className={styles.wsPill}>{chip}</span>
      </div>
      <div className={styles.wsTlBody}>{children}</div>
    </li>
  );
}

// Which design pieces the current walkthrough step exercises, so the panel follows the story.
const DESIGN_FOCUS = { queue: 1, race: 2, settle: 5, glpost: 5, statement: 5, match: 5, verify: 6 };

/** The right-hand panel: the MongoDB design behind the guard, as a timeline that follows the step. */
export function DesignPanel({ live, stepKey }) {
  const guard = live?.guard || FALLBACK_GUARD;
  const focus = DESIGN_FOCUS[stepKey] || 0;
  const raceActive = stepKey === "race";
  const at = (n) => ({ active: n === focus || (raceActive && n <= 4 && n >= 2), done: n < focus });
  return (
    <div className={styles.wsPanel}>
      <div className={styles.wsPanelHead}>
        <Icon glyph="Database" size={14} />
        <span>How it is built on MongoDB</span>
        {live && <Badge variant="green">Live</Badge>}
      </div>
      <ol className={styles.wsTl}>
        <Step title="Balance lives on one document" chip="Model" {...at(1)}>
          The invariant and the money it protects sit in <b>one</b> <InlineCode>accounts</InlineCode> document, so every wire writes the same document and MongoDB sees them collide.
          <CodeBlock label="accounts">{`{
  "accountId": "ACC-acc10004",
  "balance": { "current": 200, "available": 200, "hold": 0 }
}`}</CodeBlock>
        </Step>
        <Step title="The check is in the filter" chip="Stage 3" {...at(2)}>
          Write skew needs a gap between read and write. Here the check and the decrement are one atomic operation.
          <CodeBlock label="findOneAndUpdate">{`filter: ${pretty(guard.filter)}
update: ${pretty(stripStamps(guard.update))}`}</CodeBlock>
        </Step>
        <Step title="Collisions retry, misses refuse" chip="Stage 3" {...at(3)}>
          Concurrent writers raise <InlineCode>WriteConflict</InlineCode> (112); <InlineCode>with_transaction</InlineCode> retries on fresh state. No match means insufficient funds, and nothing is written.
        </Step>
        <Step title="Debit in one transaction" chip="Stage 5" {...at(4)}>
          Release hold, guarded debit, credit receiver, insert <InlineCode>transactions</InlineCode> record. All commit, or none do.
        </Step>
        <Step title="Downstream by change stream" chip="Stage 7–8" {...at(5)}>
          Settlement emits a change event; the ledger journals it and reconciliation matches it. Only committed wires appear.
        </Step>
      </ol>
    </div>
  );
}

const OPENING = 200;
const AMOUNT = 80;
const WIRES = 3;

function Kpi({ label, value, hint, tone = "neutral" }) {
  return (
    <div className={`${styles.wsKpi} ${styles[`wsTone_${tone}`]}`}>
      <span className={styles.wsLabel}>{label}</span>
      <span className={styles.wsKpiValue}>{value}</span>
      {hint && <span className={styles.wsKpiHint}>{hint}</span>}
    </div>
  );
}

/** Title, story and the four numbers that matter, as KPI tiles. */
export function RaceHeader({ scenario, report, queued }) {
  const s = report?.summary;
  const state = s ? (s.invariantHeld ? ["green", "Invariant held"] : ["red", "Invariant broken"]) : queued ? ["blue", "3 wires queued"] : ["lightgray", "Not queued yet"];
  return (
    <div className={styles.wsHero}>
      <div className={styles.wsHeroTop}>
        <div>
          <span className={styles.wsEyebrow}>Write skew · {scenario.key}</span>
          <h2 className={styles.wsTitle}>{scenario.title}</h2>
          <p className={styles.wsStory}>{scenario.story}</p>
        </div>
        <div className={styles.wsHeroSide}>
          <Badge variant={state[0]}>{state[1]}</Badge>
          <span className={styles.wsInvariant}><Icon glyph="Lock" size={12} />{scenario.invariant}</span>
        </div>
      </div>
      <div className={styles.wsKpis}>
        <Kpi label="Opening balance" value={usd(OPENING)} hint="ACC-acc10004" />
        <Kpi label="Demand" value={usd(WIRES * AMOUNT)} hint={`${WIRES} wires × ${usd(AMOUNT)}`} tone="warn" />
        <Kpi label="Wires reserved" value={s ? `${s.winners} / ${WIRES}` : `– / ${WIRES}`} hint={s ? `${s.totalWriteConflicts} write conflicts retried` : "after the race"} tone="info" />
        <Kpi label="Available" value={usd(s ? s.finalAvailable : OPENING)} hint={s ? "never below zero" : "before the race"} tone={s ? (s.invariantHeld ? "good" : "bad") : "neutral"} />
      </div>
    </div>
  );
}

/**
 * The USD 200 balance as a bar, one segment per wire. Before the race the third wire spills past
 * zero (the demand every read saw); after it, winners fill the bar and the refused wire stays out.
 */
export function BalanceGauge({ report }) {
  const attempts = report?.attempts || Array.from({ length: WIRES }, (_, i) => ({ worker: i + 1, outcome: null }));
  const scale = WIRES * AMOUNT;
  const pct = (v) => `${(v / scale) * 100}%`;
  return (
    <div className={styles.wsCard}>
      <div className={styles.wsCardHead}>
        <span className={styles.wsLabel}>Balance under contention</span>
        <span className={styles.wsLegend}>
          <i className={styles.segPending} />queued <i className={styles.segOk} />reserved <i className={styles.segRefused} />refused
        </span>
      </div>
      <div className={styles.wsBar}>
        <div className={styles.wsOverdraft} style={{ left: pct(OPENING) }} />
        {attempts.map((a, i) => {
          const tone = !a.outcome ? styles.segPending : a.outcome === "RESERVED" ? styles.segOk : styles.segRefused;
          return (
            <div key={a.worker} className={`${styles.wsSeg} ${tone}`} style={{ left: pct(i * AMOUNT), width: pct(AMOUNT) }}>
              Wire {a.worker} · {usd(AMOUNT)}
            </div>
          );
        })}
      </div>
      <div className={styles.wsScale}>
        <span>USD 0 spent</span>
        <span style={{ left: pct(OPENING) }} className={styles.wsScaleZero}>USD 200 · balance exhausted</span>
        <span>USD 240</span>
      </div>
    </div>
  );
}

function RaceRow({ label, verdict, tone }) {
  return (
    <li className={`${styles.wsRow} ${styles[`wsTone_${tone}`]}`}>
      <Icon glyph={tone === "good" ? "Checkmark" : tone === "bad" ? "XWithCircle" : tone === "warn" ? "Warning" : "Clock"} size={12} />
      <span className={styles.wsRowLabel}>{label}</span>
      <span className={styles.wsRowVerdict}>{verdict}</span>
    </li>
  );
}

/**
 * Why the naive design fails and the guarded one does not. The left card is illustrative (the
 * anomaly, never run); the right card is filled from the live race report.
 */
export function SkewContrast({ report }) {
  const live = (a) => {
    if (a.outcome === "RESERVED") return [`${a.writeConflicts ? `retried ×${a.writeConflicts} → ` : ""}matched, −${AMOUNT}`, "good"];
    if (a.outcome === "ERROR") return ["error", "bad"];
    return [`${a.writeConflicts ? `retried ×${a.writeConflicts} → ` : ""}no match, refused`, "warn"];
  };
  return (
    <div className={styles.wsCompare}>
      <div className={`${styles.wsCard} ${styles.wsAccent} ${styles.wsTone_bad}`}>
        <div className={styles.wsCardHead}>
          <span className={styles.wsCardTitle}>Read, then write</span>
          <span className={styles.wsPill}>Illustration · not run</span>
        </div>
        <CodeBlock>{`acct = accounts.find_one({...})          # every wire sees 200
if acct["available"] >= 80:              # all three pass
    accounts.update_one({...}, {"$set": {...}})`}</CodeBlock>
        <ul className={styles.wsRows}>
          {[1, 2, 3].map((w) => <RaceRow key={w} label={`Wire ${w}`} verdict="read 200 → passes → writes" tone="bad" />)}
        </ul>
        <div className={styles.wsOutcome}>Available ends at {usd(OPENING - WIRES * AMOUNT)}</div>
      </div>
      <div className={`${styles.wsCard} ${styles.wsAccent} ${styles.wsTone_good}`}>
        <div className={styles.wsCardHead}>
          <span className={styles.wsCardTitle}>Check inside the write</span>
          {report ? <Badge variant="green">Live</Badge> : <span className={styles.wsPill}>MongoDB</span>}
        </div>
        <CodeBlock>{`accounts.find_one_and_update(
  {"balance.available": {"$gte": 80}},
  {"$inc": {"balance.available": -80, "balance.hold": 80}})`}</CodeBlock>
        <ul className={styles.wsRows}>
          {report
            ? report.attempts.map((a) => { const [v, t] = live(a); return <RaceRow key={a.worker} label={`Wire ${a.worker}`} verdict={v} tone={t} />; })
            : [1, 2, 3].map((w) => <RaceRow key={w} label={`Wire ${w}`} verdict="waiting for the race" tone="neutral" />)}
        </ul>
        <div className={styles.wsOutcome}>
          {report ? `Available ends at ${usd(report.summary.finalAvailable)}` : `Expected: 2 paid, 1 refused, ${usd(OPENING - 2 * AMOUNT)} left`}
        </div>
      </div>
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
