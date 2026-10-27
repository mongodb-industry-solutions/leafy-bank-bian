"use client";

/**
 * Presentational pieces of the back-office approvals desk (stats tile, queue
 * item, request detail), plus the helpers that turn engine records and loan/
 * quote records into one display shape. State, data fetching and the
 * approve/deny flow live in app/back-office/page.js.
 */

import {
  Avatar,
  Badge,
  Button,
  Callout,
  Meter,
  TextArea,
} from "@via-ds/components";
import { useState } from "react";

import styles from "./BackOffice.module.css";

const BAND_LABELS = { LOW_RISK: "Low risk", MEDIUM_RISK: "Medium risk", HIGH_RISK: "High risk" };
const BAND_TONES = { LOW_RISK: "good", MEDIUM_RISK: "warn", HIGH_RISK: "bad" };

const SEVERITY_LABELS = { concern: "Concern", opportunity: "Opportunity", info: "Info" };
const SEVERITY_BADGE = { concern: "error", opportunity: "success", info: "info" };

// One-tap reviewer notes: they only fill the notes box, which stays editable.
const NOTE_SNIPPETS = {
  approved: ["Affordability checks out.", "Income and balances verified.", "Approved within policy."],
  denied: ["Affordability too tight.", "Insufficient liquidity.", "Outside lending policy."],
};

// Fields of the review payload that get dedicated treatment; anything else
// still shows under "More details", so no context is dropped.
const CONSUMED_FIELDS = new Set([
  "action",
  "task_id",
  "policy_type",
  "holder_name",
  "customer_username",
  "customer_id",
  "loan_number",
  "quote_id",
  "amount",
  "term_months",
  "annual_rate_percent",
  "monthly_payment",
  "total_repayable",
  "declared_income",
  "premium",
  "coverage",
  "deductible",
  "car_model",
  "car_year",
  "financial_summary",
  "reason",
  "conversation_summary",
  "message",
  "response_schema",
  "score",
  "risk_band",
  "recommendation",
  "score_factors",
]);

// ── formatting helpers ───────────────────────────────────────────────

function humanizeKey(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatMoney(amount, currency) {
  if (amount == null) return null;
  return `${Number(amount).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency || ""}`.trim();
}

// Display formatting for the review fields; everything else is shown as-is.
function formatValue(key, value) {
  if (value === null || value === undefined || value === "") return "not on file";
  if (key === "annual_rate_percent") return `${value}%`;
  if (key === "term_months") return `${value} months`;
  if (typeof value === "number" && /payment|repayable|income|balance/.test(key)) {
    return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function formatDateTime(value) {
  return value ? new Date(value).toLocaleString() : null;
}

export function timeAgo(value) {
  if (!value) return null;
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

export function initialsOf(name) {
  return (
    String(name || "?")
      .split(/\s+/)
      .filter(Boolean)
      .map((part) => part[0])
      .join("")
      .slice(0, 2)
      .toUpperCase() || "?"
  );
}

// The agent sends the review payload flat in suspend_context (app.suspend);
// older platform-generated reviews nest it under review_presentation.
function reviewPresentation(suspendContext) {
  return suspendContext?.review_presentation || suspendContext || {};
}

// suspend_context.allowed_decisions isn't a guaranteed field (the platform
// contract only promises it "if present") — when present, hide whichever
// button it excludes rather than assume both are always valid.
function isDecisionAllowed(allowedDecisions, decision) {
  if (!Array.isArray(allowedDecisions) || allowedDecisions.length === 0) return true;
  const normalized = allowedDecisions.map((d) => String(d).toLowerCase());
  return normalized.some((d) => d.startsWith(decision.slice(0, 6))); // "approv"/"deni" prefix match
}

// List-item field names beyond execution_id/suspend_reason/suspend_context
// aren't documented, so read defensively rather than assume one exact shape.
export function normalizeReview(item) {
  return {
    executionId: item.execution_id,
    suspendReason: item.suspend_reason,
    suspendContext: item.suspend_context || {},
    submittedAt: item.created_at || item.submitted_at || item.updated_at || null,
    userId: item.user_id || item.userId || null,
  };
}

// ── one shape for pending and decided requests ───────────────────────

export function itemFromPending(review) {
  const p = reviewPresentation(review.suspendContext);
  const kind =
    p.action === "review_personal_loan" ? "loan" : p.action === "review_policy_purchase" ? "insurance" : "other";
  return {
    key: `pending:${review.executionId}`,
    status: "pending",
    kind,
    review,
    p,
    customer: p.holder_name || review.userId || "Customer",
    username: p.customer_username || review.userId || null,
    title: p.policy_type || review.suspendReason || "Review request",
    headline: kind === "insurance" ? p.premium : p.amount,
    reference: p.loan_number || p.quote_id || review.executionId,
    when: review.submittedAt,
  };
}

export function itemFromHistory(h) {
  const isLoan = h.kind === "loan";
  return {
    key: h.key,
    status: h.decision,
    kind: h.kind,
    h,
    customer: h.customer_name || h.customer_username || h.customer_id || "Customer",
    username: h.customer_username || null,
    title: isLoan
      ? `Personal loan - ${formatMoney(h.amount, h.currency)} / ${h.term_months} months`
      : `${h.insurance_type || "Insurance"} - ${h.coverage || ""}`.trim(),
    headline: isLoan ? formatMoney(h.amount, h.currency) : h.premium,
    reference: isLoan ? h.loan_number : h.quote_id,
    when: h.decided_at,
  };
}

function tilesFor(item) {
  const tiles =
    item.status === "pending"
      ? item.kind === "loan"
        ? [
            ["Amount", item.p.amount],
            ["Term", item.p.term_months != null ? formatValue("term_months", item.p.term_months) : null],
            ["Annual rate", item.p.annual_rate_percent != null ? formatValue("annual_rate_percent", item.p.annual_rate_percent) : null],
            ["Monthly payment", item.p.monthly_payment != null ? formatValue("monthly_payment", item.p.monthly_payment) : null],
            ["Total repayable", item.p.total_repayable != null ? formatValue("total_repayable", item.p.total_repayable) : null],
            ["Declared income", item.p.declared_income],
          ]
        : item.kind === "insurance"
          ? [
              ["Premium", item.p.premium],
              ["Coverage", item.p.coverage],
              ["Deductible", item.p.deductible],
              ["Vehicle", item.p.car_model ? `${item.p.car_model} (${item.p.car_year})` : null],
            ]
          : []
      : item.kind === "loan"
        ? [
            ["Amount", formatMoney(item.h.amount, item.h.currency)],
            ["Term", item.h.term_months ? `${item.h.term_months} months` : null],
            ["Annual rate", item.h.annual_rate_percent != null ? `${item.h.annual_rate_percent}%` : null],
            ["Monthly payment", formatMoney(item.h.monthly_payment, item.h.currency)],
            ["Total repayable", formatMoney(item.h.total_repayable, item.h.currency)],
          ]
        : [
            ["Premium", item.h.premium],
            ["Coverage", item.h.coverage],
            ["Deductible", item.h.deductible],
            ["Vehicle", item.h.car_model ? `${item.h.car_model} (${item.h.car_year})` : null],
          ];
  return tiles.filter(([, value]) => value);
}

// ── small presentational pieces ──────────────────────────────────────

function StatusBadge({ item }) {
  if (item.status === "pending") return <Badge variant="warning">Pending review</Badge>;
  if (item.status === "approved") return <Badge variant="success">Approved</Badge>;
  return <Badge variant="error">Denied</Badge>;
}

function FollowUpBadge({ item }) {
  if (item.status !== "approved" || !item.h) return null;
  const done = item.kind === "loan" ? item.h.funded : item.h.policy_created;
  const label =
    item.kind === "loan" ? (done ? "Paid out" : "Payout pending") : done ? "Policy created" : "Policy not created";
  return <Badge variant={done ? "success" : "warning"}>{label}</Badge>;
}

export function KpiTile({ label, value, hint, tone }) {
  return (
    <div className={`${styles.kpi} ${tone ? styles[`kpi_${tone}`] : ""}`}>
      <div className={styles.kpiValue}>{value}</div>
      <div className={styles.kpiLabel}>{label}</div>
      {hint && <div className={styles.kpiHint}>{hint}</div>}
    </div>
  );
}

export function QueueItem({ item, isSelected, onSelect }) {
  const waiting = item.status === "pending" ? timeAgo(item.when) : null;
  return (
    <button
      type="button"
      className={`${styles.queueItem} ${isSelected ? styles.queueItemActive : ""} ${
        item.status === "pending" ? styles.queueItemPending : ""
      }`}
      onClick={onSelect}
      aria-current={isSelected}
    >
      <Avatar initials={initialsOf(item.customer)} size={36} alt="" />
      <div className={styles.queueMain}>
        <div className={styles.queueTop}>
          <span className={styles.queueName}>{item.customer}</span>
          {item.headline && <span className={styles.queueAmount}>{item.headline}</span>}
        </div>
        <div className={styles.queueTitle}>{item.title}</div>
        <div className={styles.queueMeta}>
          <StatusBadge item={item} />
          <FollowUpBadge item={item} />
          {waiting && <span className={styles.queueWaiting}>waiting {waiting.replace(" ago", "")}</span>}
          {!waiting && item.when && <span className={styles.queueWhen}>{timeAgo(item.when)}</span>}
        </div>
      </div>
    </button>
  );
}

// Affordability score: a ring with the number, the band spelled out in text
// (never colour alone) and the engine's recommendation.
function ScoreRing({ score, band }) {
  const tone = BAND_TONES[band] || "warn";
  return (
    <div className={styles.scoreRingWrap}>
      <div
        className={`${styles.scoreRing} ${styles[`ring_${tone}`]}`}
        style={{ "--pct": Math.max(0, Math.min(100, Number(score) || 0)) }}
        role="img"
        aria-label={`Score ${score} out of 100`}
      >
        <div className={styles.scoreRingInner}>
          <span className={styles.scoreNumber}>{score}</span>
          <span className={styles.scoreOutOf}>/ 100</span>
        </div>
      </div>
    </div>
  );
}

function RiskAssessment({ presentation }) {
  const factors = Array.isArray(presentation.score_factors) ? presentation.score_factors : [];
  if (presentation.score == null && factors.length === 0) return null;
  const band = presentation.risk_band;
  const tone = BAND_TONES[band] || "warn";
  return (
    <section className={styles.panelCard}>
      <h3 className={styles.sectionTitle}>Risk assessment</h3>
      <div className={styles.riskTop}>
        {presentation.score != null && <ScoreRing score={presentation.score} band={band} />}
        <div className={styles.riskText}>
          <span className={`${styles.bandPill} ${styles[`band_${tone}`]}`}>{BAND_LABELS[band] || band}</span>
          {presentation.recommendation && <p className={styles.recommendation}>{presentation.recommendation}</p>}
          <p className={styles.muted}>The score informs the decision; it never makes it. Each factor shows the data behind it.</p>
        </div>
      </div>

      <div className={styles.factors}>
        {factors.map((f) => {
          const ratio = f.max_points ? f.points / f.max_points : 0;
          return (
            <div key={f.name} className={styles.factor}>
              <div className={styles.factorTop}>
                <span className={styles.factorName}>{f.name}</span>
                <span className={styles.factorPoints}>
                  {f.points} / {f.max_points}
                </span>
              </div>
              <Meter
                aria-label={f.name}
                value={f.points}
                minValue={0}
                maxValue={f.max_points}
                variant={ratio >= 0.7 ? "success" : ratio >= 0.4 ? "warning" : "danger"}
              />
              <div className={styles.factorSummary}>{f.summary}</div>
              {f.data && (
                <details className={styles.factorData}>
                  <summary>Underlying data</summary>
                  <dl>
                    {Object.entries(f.data).map(([k, v]) => (
                      <div key={k} className={styles.factorDataRow}>
                        <dt>{humanizeKey(k)}</dt>
                        <dd>{v === null || v === undefined ? "not on file" : String(v)}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

// The customer's financial-wellness analysis at the time of the request
// (computed fresh by the agent). Concerns and opportunities lead; the
// remaining informational lines are one click away, not dropped.
function FinancialPicture({ summary }) {
  const [showAll, setShowAll] = useState(false);
  const insights = Array.isArray(summary?.insights) ? summary.insights : [];
  if (insights.length === 0) return null;
  const concerns = insights.filter((i) => i.severity === "concern").length;
  const highlighted = insights.filter((i) => i.severity !== "info");
  const infoCount = insights.length - highlighted.length;
  const shown = showAll ? insights : highlighted.length > 0 ? highlighted : insights.slice(0, 3);
  const hiddenCount = insights.length - shown.length;

  return (
    <section className={styles.panelCard}>
      <h3 className={styles.sectionTitle}>Customer financial picture</h3>
      <div className={styles.pictureStats}>
        {summary.total_balance != null && (
          <div className={styles.pictureStat}>
            <span className={styles.pictureStatValue}>
              {formatValue("balance", summary.total_balance)} {summary.currency || ""}
            </span>
            <span className={styles.pictureStatLabel}>Total balance</span>
          </div>
        )}
        <div className={styles.pictureStat}>
          <span className={`${styles.pictureStatValue} ${concerns > 0 ? styles.textBad : styles.textGood}`}>
            {concerns > 0 ? `${concerns} concern${concerns === 1 ? "" : "s"}` : "No concerns"}
          </span>
          <span className={styles.pictureStatLabel}>Flagged</span>
        </div>
        {summary.computed_at && (
          <div className={styles.pictureStat}>
            <span className={styles.pictureStatValue}>{timeAgo(summary.computed_at)}</span>
            <span className={styles.pictureStatLabel}>Analysed</span>
          </div>
        )}
      </div>

      <div className={styles.insightList}>
        {shown.map((i, idx) => (
          <div key={`${i.name}-${idx}`} className={styles.insightRow}>
            <Badge variant={SEVERITY_BADGE[i.severity] || "info"}>{SEVERITY_LABELS[i.severity] || i.severity}</Badge>
            <span className={styles.insightText}>
              <strong>{humanizeKey(i.name)}:</strong> {i.description}
            </span>
          </div>
        ))}
      </div>

      {(hiddenCount > 0 || showAll) && infoCount > 0 && (
        <Button variant="tertiary" size="small" onPress={() => setShowAll((v) => !v)}>
          {showAll ? "Show fewer" : `Show ${hiddenCount} more detail${hiddenCount === 1 ? "" : "s"}`}
        </Button>
      )}
    </section>
  );
}

function Quote({ label, text }) {
  if (!text) return null;
  return (
    <section className={styles.panelCard}>
      <h3 className={styles.sectionTitle}>{label}</h3>
      <blockquote className={styles.quote}>{text}</blockquote>
    </section>
  );
}

// Where a decided request stands: submitted → decided → follow-up
// (payout for a loan, policy for an insurance purchase).
function Timeline({ item }) {
  const h = item.h;
  const approved = item.status === "approved";
  const isLoan = item.kind === "loan";
  const followDone = isLoan ? h.funded : h.policy_created;
  const steps = [
    { label: "Submitted", at: h.submitted_at, state: "done" },
    { label: approved ? "Approved" : "Denied", at: h.decided_at, state: approved ? "done" : "denied" },
  ];
  if (approved) {
    steps.push({
      label: isLoan ? "Paid out" : "Policy created",
      at: isLoan ? h.disbursement?.at : null,
      state: followDone ? "done" : "waiting",
      detail: isLoan
        ? h.disbursement
          ? `${formatMoney(h.disbursement.amount, h.currency)} to ${h.disbursement.account_id}`
          : "Not yet paid out"
        : h.policy_number || "Not created yet",
    });
  }
  return (
    <section className={styles.panelCard}>
      <h3 className={styles.sectionTitle}>Progress</h3>
      <ol className={styles.timeline}>
        {steps.map((step) => (
          <li key={step.label} className={`${styles.step} ${styles[`step_${step.state}`]}`}>
            <span className={styles.stepDot} aria-hidden="true" />
            <span className={styles.stepLabel}>{step.label}</span>
            {formatDateTime(step.at) && <span className={styles.stepAt}>{formatDateTime(step.at)}</span>}
            {step.detail && <span className={styles.stepAt}>{step.detail}</span>}
          </li>
        ))}
      </ol>
    </section>
  );
}

// ── the selected request ─────────────────────────────────────────────

export function DetailPanel({ item, notes, onNotes, isResolving, error, onDecide }) {
  const isPending = item.status === "pending";
  const presentation = item.p || {};
  const tiles = tilesFor(item);
  const extraFields = isPending
    ? Object.entries(presentation).filter(([k, v]) => !CONSUMED_FIELDS.has(k) && v != null && typeof v !== "object")
    : [];
  const allowed = item.review?.suspendContext?.allowed_decisions;
  const loanOrInsurance = item.kind === "loan" || item.kind === "insurance";
  const riskPresentation =
    item.kind === "loan" && !isPending
      ? {
          score: item.h.score,
          risk_band: item.h.risk_band,
          recommendation: item.h.recommendation,
          score_factors: item.h.score_factors,
        }
      : presentation;

  return (
    <div className={styles.detail}>
      <header className={styles.detailHeader}>
        <Avatar initials={initialsOf(item.customer)} size={52} alt="" />
        <div className={styles.detailWho}>
          <h2 className={styles.detailName}>{item.customer}</h2>
          <div className={styles.detailSub}>
            {[item.username && `@${item.username}`, presentation.customer_id || item.h?.customer_id]
              .filter(Boolean)
              .join(" · ")}
          </div>
        </div>
        <div className={styles.detailBadges}>
          <StatusBadge item={item} />
          <FollowUpBadge item={item} />
        </div>
      </header>

      <div className={styles.detailProduct}>
        <span className={styles.productLabel}>{item.title}</span>
        <span className={styles.productRef}>{item.reference}</span>
        {isPending && item.when && <span className={styles.productRef}>Submitted {timeAgo(item.when)}</span>}
      </div>

      {tiles.length > 0 && (
        <div className={styles.tiles}>
          {tiles.map(([label, value], i) => (
            <div key={label} className={`${styles.tile} ${i === 0 ? styles.tileHero : ""}`}>
              <span className={styles.tileLabel}>{label}</span>
              <span className={styles.tileValue}>{value}</span>
            </div>
          ))}
        </div>
      )}

      {item.kind === "loan" && <RiskAssessment presentation={riskPresentation} />}
      {isPending && <FinancialPicture summary={presentation.financial_summary} />}
      {isPending && <Quote label="Why this needs review" text={presentation.reason} />}
      {isPending && <Quote label="Conversation summary" text={presentation.conversation_summary} />}
      {!isPending && loanOrInsurance && <Timeline item={item} />}
      {!isPending && <Quote label="Reviewer notes" text={item.h.reviewer_notes || "No notes were left."} />}

      {extraFields.length > 0 && (
        <section className={styles.panelCard}>
          <h3 className={styles.sectionTitle}>More details</h3>
          <div className={styles.extraGrid}>
            {extraFields.map(([key, value]) => (
              <div key={key}>
                <span className={styles.tileLabel}>{humanizeKey(key)}</span>
                <span className={styles.extraValue}>{formatValue(key, value)}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {isPending && (
        <section className={styles.decisionBar}>
          {error && (
            <Callout variant="warning" className={styles.decisionError}>
              {error}
            </Callout>
          )}
          <div className={styles.notesLabel}>
            Reviewer notes <span className={styles.muted}>(sent with your decision and shown to the customer's assistant)</span>
          </div>
          <TextArea
            aria-label="Reviewer notes"
            placeholder="Add a note for the record…"
            value={notes}
            onChange={onNotes}
            isDisabled={isResolving}
            className={styles.notesInput}
          />
          {["approved", "denied"].map((group) => (
            <div key={group} className={styles.snippets}>
              <span className={styles.snippetLabel}>{group === "approved" ? "If approving" : "If denying"}</span>
              {NOTE_SNIPPETS[group].map((snippet) => (
                <Button
                  key={snippet}
                  variant="tertiary"
                  size="small"
                  isDisabled={isResolving}
                  onPress={() => onNotes(notes ? `${notes.trimEnd()} ${snippet}` : snippet)}
                >
                  + {snippet}
                </Button>
              ))}
            </div>
          ))}
          <div className={styles.decisionActions}>
            {isDecisionAllowed(allowed, "approved") && (
              <Button variant="primary" isDisabled={isResolving} onPress={() => onDecide(item.review, "approved")}>
                {isResolving ? "Resuming…" : "Approve"}
              </Button>
            )}
            {isDecisionAllowed(allowed, "denied") && (
              <Button variant="secondaryDanger" isDisabled={isResolving} onPress={() => onDecide(item.review, "denied")}>
                {isResolving ? "Resuming…" : "Deny"}
              </Button>
            )}
            {isResolving && (
              <span className={styles.resuming}>Sending your decision and waiting for the assistant to tell the customer…</span>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

