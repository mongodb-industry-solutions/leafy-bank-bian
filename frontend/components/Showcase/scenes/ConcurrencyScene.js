"use client";

// The W1 walkthrough's panels: the balance strip and one card per wire (evidence), the MongoDB
// setup beside the console (aside), and the verdict (result).

import { useEffect, useState } from "react";
import Link from "next/link";
import Badge from "@leafygreen-ui/badge";
import Icon from "@leafygreen-ui/icon";
import { Body, H3, InlineCode, Overline } from "@leafygreen-ui/typography";
import { reservedAt, usd } from "../concurrencyRunLog";
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
// Op tones mapped onto the page-wide tone scale.
const STEP_TONE = { ok: "resolved", wait: "waiting", refuse: "neutral", info: "info" };
const OUTCOME_BADGE = {
  RESERVED: ["green", "Reserved"],
  REFUSED_INSUFFICIENT_FUNDS: ["lightgray", "Refused"],
  ERROR: ["red", "Error"],
};

function StepRow({ step }) {
  return (
    <li className={`${styles.opsStep} ${styles[`tone_${STEP_TONE[step.tone]}`]}`}>
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

function PaymentOpsCard({ attempt, shown, payment }) {
  const groups = groupsOf(attempt, attempt.amount);
  const total = stepCount(attempt, attempt.amount);
  const [variant, label] = OUTCOME_BADGE[attempt.outcome] || OUTCOME_BADGE.ERROR;
  // The read is step 1; every group step after it counts toward the shared reveal.
  let budget = shown - 1;
  return (
    <div className={styles.opsCard}>
      <div className={styles.opsHead}>
        <Body as="span" className={styles.opsTitle}>Wire {attempt.worker} · {usd(attempt.amount)}</Body>
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
  const total = Math.max(...report.attempts.map((a) => stepCount(a, a.amount)));
  const shown = useStepReveal(report, total);
  return (
    <div className={styles.opsGrid}>
      {report.attempts.map((a) => (
        <PaymentOpsCard key={a.worker} attempt={a} shown={shown} payment={payments[a.paymentId]} />
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

function CodeBlock({ label, children }) {
  return (
    <div className={styles.w1Code}>
      {label && <span className={styles.w1CodeLabel}>{label}</span>}
      <pre className={styles.opsPre}>{children}</pre>
    </div>
  );
}

function Step({ title, chip, active, done, children }) {
  const state = active ? styles.w1StepActive : done ? "" : styles.w1StepIdle;
  return (
    <li className={`${styles.w1Step} ${state}`} aria-current={active ? "step" : undefined}>
      <span className={styles.w1StepDot} aria-hidden="true" />
      <div className={styles.w1StepHead}>
        <span className={styles.w1StepTitle}>{title}</span>
        <Badge variant="lightgray">{chip}</Badge>
      </div>
      <Body as="div" className={styles.w1StepBody}>{children}</Body>
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
    <aside className={styles.aiPanel} aria-label="MongoDB design">
      <header className={styles.aiHead}>
        <span className={`${styles.aiTitle} ${styles.w1PanelTitle}`}><Icon glyph="Database" size={16} />MongoDB design</span>
        <Badge variant={live ? "green" : "lightgray"}>{live ? "Live" : "No agent"}</Badge>
      </header>
      <div className={styles.aiTouched} aria-label="MongoDB collections this scenario writes">
        <Icon glyph="Database" size={12} />
        <span className={styles.mongoChips}>
          <span className={`${styles.mongoChip} ${styles.mongoWrite}`}>accounts</span>
          <span className={`${styles.mongoChip} ${styles.mongoWrite}`}>payments</span>
          <span className={`${styles.mongoChip} ${styles.mongoWrite}`}>transactions</span>
        </span>
      </div>
      <ol className={styles.w1Steps}>
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
    </aside>
  );
}

const OPENING = 200;
// Wire n sends AMOUNTS[n - 1]; mirrors the backend. Any two fit in the opening balance, all three do not.
const AMOUNTS = [80, 110, 90];
const WIRES = AMOUNTS.length;
const DEMAND = AMOUNTS.reduce((t, n) => t + n, 0);

function Fact({ glyph, children, mono }) {
  return (
    <div className={styles.payFact}>
      <Icon glyph={glyph} size={14} className={styles.payFactIcon} />
      <Body as="span" className={mono ? styles.mono : undefined}>{children}</Body>
    </div>
  );
}

/** The race at a glance, in the same card the other walkthroughs use for the payment. */
export function RaceCard({ scenario, report, queued }) {
  const s = report?.summary;
  const state = s ? (s.invariantHeld ? ["green", "Invariant held"] : ["red", "Invariant broken"]) : queued ? ["blue", "3 wires queued"] : ["lightgray", "Not queued yet"];
  return (
    <div className={`${styles.railCard} ${styles.toneBorder} ${styles.payCard}`}>
      <H3 as="p" className={styles.paneTitle}>USD {AMOUNTS.join(" + ")} vs {usd(OPENING)}</H3>
      <Fact glyph="Building">Account <span className={styles.mono}>ACC-acc10004</span> · opening {usd(OPENING)}</Fact>
      <Fact glyph="Lock" mono>{scenario.invariant}</Fact>
      <Fact glyph="Coin">
        {s
          ? <>{s.winners} / {WIRES} reserved · {usd(s.finalAvailable)} left <span className={styles.muted}>· {s.totalWriteConflicts} conflicts retried</span></>
          : <span className={styles.muted}>Demand {usd(DEMAND)}, before the race</span>}
      </Fact>
      <div className={styles.chipRow}><Badge variant={state[0]}>{state[1]}</Badge></div>
    </div>
  );
}

const joinNames = (wires) => wires.map((a) => `Wire ${a.worker}`).join(" and ");

/** The result line under the bar, once the race has decided. */
function raceOutcome(report) {
  const paid = report.attempts.filter((a) => a.outcome === "RESERVED");
  const refused = report.attempts.filter((a) => a.outcome !== "RESERVED");
  const refusedText = refused.length ? ` · ${joinNames(refused)} refused` : "";
  return {
    tone: report.summary.invariantHeld ? "resolved" : "alert",
    text: `${joinNames(paid)} paid${refusedText} · ${usd(report.summary.finalAvailable)} left`,
  };
}

/**
 * What the wires ask for against what the account holds, one segment per wire sized by amount.
 * Before the race the wires sit in order and the last spills into the overdraft zone; after it,
 * the winners fill the bar in the order they reserved and the refused wire is pushed past the line.
 */
export function BalanceGauge({ report }) {
  const queued = AMOUNTS.map((amount, i) => ({ worker: i + 1, amount, outcome: null }));
  const attempts = report ? [...report.attempts].sort((a, b) => reservedAt(a) - reservedAt(b) || a.worker - b.worker) : queued;
  const pct = (v) => `${(v / DEMAND) * 100}%`;
  const outcome = report && raceOutcome(report);
  let offset = 0;
  return (
    <div className={styles.railCard}>
      <div className={styles.w1CardHead}>
        <Overline>Requested vs available</Overline>
        <span className={styles.w1Legend}>
          <i className={styles.segPending} />waiting <i className={styles.segOk} />paid <i className={styles.segRefused} />refused
        </span>
      </div>
      <Body as="p" className={styles.w1Question}>
        Three wires ask for {usd(DEMAND)}. The account has {usd(OPENING)}. Who gets paid?
      </Body>
      <div className={styles.w1Bar}>
        {/* Drawn over the segments, so the part of a wire past the balance reads as overdraft. */}
        <div className={styles.w1Overdraft} style={{ "--at": pct(OPENING) }} />
        {attempts.map((a) => {
          const tone = !a.outcome ? styles.segPending : a.outcome === "RESERVED" ? styles.segOk : styles.segRefused;
          const left = offset;
          offset += a.amount;
          return (
            <div key={a.worker} className={`${styles.w1Seg} ${tone}`} style={{ "--at": pct(left), "--size": pct(a.amount) }}>
              Wire {a.worker} · {usd(a.amount)}
            </div>
          );
        })}
      </div>
      <div className={styles.w1Scale}>
        <span>USD 0</span>
        <span className={styles.w1ScaleZero} style={{ "--at": pct(OPENING) }}>{usd(OPENING)} available</span>
        <span className={styles.w1ScaleOver}>overdraft · must never happen</span>
      </div>
      {outcome && (
        <div className={`${styles.w1Row} ${styles[`tone_${outcome.tone}`]}`}>
          <Icon glyph={outcome.tone === "resolved" ? "Checkmark" : "XWithCircle"} size={12} className={styles.w1RowIcon} />
          <span>{outcome.text}</span>
        </div>
      )}
    </div>
  );
}

function ContrastHalf({ tone, title, headline, why, code }) {
  return (
    <div className={`${styles.w1Half} ${styles[`tone_${tone}`]}`}>
      <Overline>{title}</Overline>
      <H3 as="p" className={styles.w1Headline}>{headline}</H3>
      <Body as="p" className={styles.w1Why}>{why}</Body>
      <details className={styles.opsCode}>
        <summary>Show code</summary>
        <pre className={styles.opsPre}>{code}</pre>
      </details>
    </div>
  );
}

/**
 * Why the race came out right: the same three wires without a guard (illustrative, never run)
 * against the live result with it. Shown only after the race, so it explains rather than spoils.
 */
export function SkewContrast({ report }) {
  if (!report) return null;
  return (
    <div className={`${styles.railCard} ${styles.w1Compare}`}>
      <ContrastHalf
        tone="alert"
        title="Without a guard"
        headline={`${usd(OPENING - DEMAND)} · overdrawn`}
        why={`Each wire checks the balance it read earlier. All three see ${usd(OPENING)}, so all three pay.`}
        code={`acct = accounts.find_one({...})          # every wire sees 200
if acct["available"] >= amount:          # 80, 110, 90: all pass
    accounts.update_one({...}, {"$set": {...}})`}
      />
      <ContrastHalf
        tone={report.summary.invariantHeld ? "resolved" : "alert"}
        title="With MongoDB's guard · live"
        headline={`${usd(report.summary.finalAvailable)} left · protected`}
        why="The check runs inside the update. Once the money is gone, the last wire finds no match and is refused."
        code={`accounts.find_one_and_update(
  {"balance.available": {"$gte": amount}},
  {"$inc": {"balance.available": -amount, "balance.hold": amount}})`}
      />
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
