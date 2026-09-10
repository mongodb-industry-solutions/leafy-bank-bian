"use client";

// The payment lifecycle deep dive — a thin horizontal mini-stepper (zone 1) over a vertical
// stepper-timeline (zone 2), per the Payment Analyst UI research (§3.3/§3.4).
//
// Why vertical primary, not the horizontal rail this used to be: a single payment is a
// single-threaded state machine, and each stage carries too much (timestamps, actor,
// checks, ISO views, double-entry legs) to fit under a node. A vertical spine gives each
// stage's detail the full width it needs, and the mini-stepper above preserves the
// left-to-right saga shape and stays the clickable summary. Clicking either scrolls the
// matching row into view and expands it. A terminal failure auto-expands the failing stage
// and dims the downstream ones.
//
// Two data sources, composed client-side and never server-side:
//   * /workflow/payments/{id}  (transactions) — stages 1-4
//   * /pipeline/trace/{id}     (ledger)       — stages 5-8
// Neither service reads the other's collections (decisions.md 2026-06-18).
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Banner from "@leafygreen-ui/banner";
import Code from "@leafygreen-ui/code";
import { Tab, Tabs } from "@leafygreen-ui/tabs";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import Tooltip from "@leafygreen-ui/tooltip";
import { Body } from "@leafygreen-ui/typography";

import styles from "./PaymentsWorkflow.module.css";
import StatusPill from "./StatusPill";
import StepUpModal from "@/components/StepUpModal/StepUpModal";
import { buildLifecycleStages, legTotals } from "./lifecycleStages";
import { usePaymentWorkflow, usePipelineTrace } from "@/lib/api/hooks";
import { coreApi } from "@/lib/api/client";
import {
  checkPillFamily,
  fmtAmount,
  fmtWhen,
} from "@/lib/paymentsWorkflow/status";

const FAILED_STATES = new Set([
  "REJECTED", "FAILED", "RETURNED", "REVERSED", "REFUNDED", "CANCELLED",
]);
const SUCCESS_TERMINAL = new Set(["SETTLED", "RECONCILED", "POSTED", "COMPLETED"]);

const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

// The ✓ belongs to the LINEAR saga (stages 1-5), where order is the fact. An independent-axis
// stage (6-8: posting / settlement / reconciliation) that reaches its own terminal state
// renders as a plain filled circle — no ✓ — so settlement finishing before posting doesn't
// drop a lone check in the middle of the sequence (research §1.4: these axes advance
// alongside the saga, not in order).
const nodeGlyph = (stage, state) =>
  state === "failed" ? "×" : state === "completed" ? (stage >= 6 ? "" : "✓") : "";

/**
 * Per-stage node state for the mini-stepper and the vertical timeline.
 *
 * Two different progress models live on this one rail, and they must not be conflated:
 *
 *   * stages 1-5 (the linear saga) — order is the fact. A stage is "completed" if a later
 *     stage was reached; the last reached one is "current" (in flight), "completed"
 *     (terminal success) or "failed" (terminal failure); later ones are "pending".
 *
 *   * stages 6-8 (posting / settlement / reconciliation) — independent axes that advance
 *     ALONGSIDE the saga, not in lockstep with it (research §1.4). Settlement can be
 *     SETTLED while posting is still not started. Their completion is the stage's OWN
 *     terminal status, never its position relative to a later stage — otherwise an
 *     un-started accounting stage is "filled in" as green just because settlement finished.
 *
 * The independent axis stages carry their own `status` (le.postingStatus, jn.status,
 * lifecycle.settlementStatus, reconciliation.overallResult, …). We key off that directly.
 */
function nodeStates(stages, paymentStatus) {
  if (!stages?.length) return [];
  let lastReached = -1;
  for (let i = stages.length - 1; i >= 0; i--) {
    if (stages[i].reached) {
      lastReached = i;
      break;
    }
  }
  const status = (paymentStatus || "").toUpperCase();
  const failed = FAILED_STATES.has(status);
  const done = SUCCESS_TERMINAL.has(status);
  return stages.map((s, i) => {
    // Independent axis (stage ≥ 6): completion is the stage's own terminal fact.
    if (s.stage >= 6) {
      const ownStatus = (s.status || "").toUpperCase();
      if (FAILED_STATES.has(ownStatus)) return "failed";
      if (SUCCESS_TERMINAL.has(ownStatus)) return "completed";
      // Reached but not terminal (PENDING, etc.): in flight. Never reached: not started.
      return s.reached ? "current" : "pending";
    }
    // Linear saga (stages 1-5): order is the fact.
    if (i < lastReached) return "completed";
    if (i === lastReached) return failed ? "failed" : done ? "completed" : "current";
    return "pending";
  });
}

/** Zone 1 — thin horizontal clickable summary that preserves the saga's left-to-right shape. */
function MiniStepper({ stages, states, selectedKey, onSelect }) {
  return (
    <div className={styles.miniStepperScroll}>
      <div className={styles.miniStepperTrack}>
        {stages.map((s, i) => (
          <Fragment key={s.key}>
            {i > 0 && (
              <span
                className={`${styles.miniConnector} ${
                  states[i - 1] === "completed" ? styles.miniConnectorDone : ""
                }`}
              />
            )}
            <button
              type="button"
              className={`${styles.miniNode} ${
                selectedKey === s.key ? styles.miniNodeActive : ""
              }`}
              onClick={() => onSelect(s.key)}
              aria-pressed={selectedKey === s.key}
              title={s.label}
            >
              <span className={`${styles.miniCircle} ${styles[`mini${cap(states[i])}`]}`}>
                {nodeGlyph(s.stage, states[i])}
              </span>
              <span className={styles.miniLabel}>{s.label}</span>
            </button>
          </Fragment>
        ))}
      </div>
    </div>
  );
}

/** Zone 2 — vertical spine of expandable rows; the selected row expands to its full detail. */
function VerticalTimeline({ stages, states, selectedKey, onSelect, payment, setRowRef, onApprove }) {
  const failedIdx = states.indexOf("failed");
  return (
    <div className={styles.timeline}>
      {stages.map((s, i) => {
        const expanded = selectedKey === s.key;
        const dimmed = failedIdx >= 0 && i > failedIdx;
        const nodeState = states[i];
        return (
          <div
            key={s.key}
            className={`${styles.timelineRow} ${dimmed ? styles.timelineRowDimmed : ""}`}
            ref={setRowRef(s.key)}
          >
            <div className={styles.spine}>
              <span className={`${styles.timelineNode} ${styles[`node${cap(nodeState)}`]}`}>
                {nodeGlyph(s.stage, nodeState)}
              </span>
              {i < stages.length - 1 && (
                <span
                  className={`${styles.timelineConnector} ${
                    nodeState === "completed" ? styles.timelineConnectorDone : ""
                  } ${i === failedIdx ? styles.timelineConnectorDashed : ""}`}
                />
              )}
            </div>
            <div className={styles.timelineContent}>
              <button
                type="button"
                className={styles.timelineRowHeader}
                onClick={() => onSelect(s.key)}
                aria-expanded={expanded}
              >
                <span className={styles.timelineStageLabel}>{s.label}</span>
                <span className={styles.timelineStageMeta}>{s.meta || "—"}</span>
                {s.status && <StatusPill status={s.status} />}
              </button>
              {nodeState === "failed" && expanded && (
                <div className={styles.failedBanner}>
                  Payment stopped at {s.label} — status {payment?.status || "unknown"}.
                </div>
              )}
              {expanded && (
                <div className={styles.timelineRowBody}>
                  <StageDetailBody
                    stage={s}
                    payment={payment}
                    onApprove={onApprove}
                  />
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function KeyValues({ rows }) {
  return (
    <table className={styles.kv}>
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}>
            <td>{k}</td>
            <td>{v ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Stage 3's before/after — Doina's L459-460 acceptance, verbatim:
 *
 *   "For an ISO 20022 wire, show the original business instruction progressively enriched:
 *    Supplier XYZ Account 123456 ↓ enrichment Supplier XYZ Ltd. 123 Business Street New York
 *    NY Account US123456… BIC ABCDUS33 Purpose: SUPP Remittance: INV-48392"
 *
 * Reuses the `.legs` grid built for ledger DR/CR. It is the same shape — two columns, one row
 * per line, a header over each — so a second grid would be a copy with different words in it.
 *
 * Reads `payments.enrichment.resolved[]`, which exists precisely because the document holds
 * one value per field: without that record the "before" column has no source at all (doc 17
 * B1). `source` is shown because "where did this value come from" is the question an operator
 * asks first.
 */
function EnrichmentDiff({ enrichment }) {
  const resolved = enrichment?.resolved || [];

  if (!enrichment) {
    return <Body className={styles.muted}>No enrichment record on this payment.</Body>;
  }
  if (!resolved.length) {
    return (
      <Body className={styles.muted}>
        Nothing required enrichment — every field was already resolved at initiation.
      </Body>
    );
  }

  return (
    <div className={styles.legs}>
      <div className={styles.legsHead}>As captured</div>
      <div className={styles.legsHead}>After enrichment</div>
      {resolved.map((r) => (
        <Fragment key={r.field}>
          <div className={styles.legRow}>
            <span>{r.field}</span>
            <span className={styles.legAmount}>{fmtEnriched(r.from)}</span>
          </div>
          <div className={styles.legRow}>
            <span className={styles.legAmount}>{fmtEnriched(r.to)}</span>
            <StatusPill family="gray">{r.source}</StatusPill>
          </div>
        </Fragment>
      ))}
    </div>
  );
}

/**
 * Stage 3's body, laid out in a controlled grid rather than the generic auto-fit
 * `detailColumns`, which crammed four blocks into as many narrow columns. Checks now
 * collapses to a summary banner by default, so it is given the full width on top (no dead
 * column under a collapsed trail); Summary and State transitions sit side by side beneath
 * it; the Progressive-enrichment diff spans the full width last, so its As-captured /
 * After-enrichment pair has room. Same controlled-grid intent as stage 2's `stageTwoGrid`.
 */
function EnrichmentBody({ stage, payment, checkList }) {
  const events = stage.data?.events || [];
  return (
    <div className={styles.stageThreeGrid}>
      <div className={styles.detailBlockWide}>
        <div className={styles.detailBlockTitle}>Checks</div>
        <Checks checks={checkList} />
      </div>
      <div className={styles.detailBlock}>
        <div className={styles.detailBlockTitle}>Summary</div>
        <KeyValues rows={summaryRows(stage, payment)} />
      </div>
      <div className={styles.detailBlock}>
        <div className={styles.detailBlockTitle}>State transitions</div>
        <StateEvents events={events} />
      </div>
      <div className={styles.detailBlockWide}>
        <div className={styles.detailBlockTitle}>Progressive enrichment</div>
        <EnrichmentDiff enrichment={stage.data?.enrichment} />
      </div>
    </div>
  );
}

/** A resolved value as one concise line. Plain scalars pass through; an object (a fee, a
 *  regulatory report) is collapsed to its naming field plus the qualifier that matters, so
 *  the "after enrichment" column reads "WIRE_FEE 25.00 USD (SHARED)" rather than a raw
 *  `k: v, k2: v2, …` dump. Arrays (e.g. regulatoryReports[]) join their items on " ; ".
 *  The full value is one click away on the raw-document toggle. */
function fmtEnriched(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) {
    if (!value.length) return "—";
    return value.map((v) => fmtEnriched(v)).join(" ; ");
  }
  if (typeof value === "object") {
    const name = value.reportType || value.type || value.name || value.code;
    if (name) {
      const extras = [];
      if (value.amount != null) extras.push(fmtAmount(value.amount, value.currency));
      if (value.status) extras.push(`(${value.status})`);
      else if (value.chargedTo) extras.push(`(${value.chargedTo})`);
      return extras.length ? `${name} ${extras.join(" ")}` : name;
    }
    return Object.entries(value)
      .filter(([, v]) => v !== null && v !== undefined && v !== "")
      .map(([k, v]) => `${k}: ${v}`)
      .join(", ");
  }
  return String(value);
}

/** Debits left, credits right, totals and a balanced verdict — DR == CR read spatially. */
function Legs({ legs }) {
  const { debit, credit, balanced } = legTotals(legs);
  const rows = Math.max(legs.debits.length, legs.credits.length);
  const cell = (e) =>
    e ? (
      <div className={styles.legRow}>
        <span>{e.code}{e.name ? ` · ${e.name}` : ""}</span>
        <span className={styles.legAmount}>{fmtAmount(e.amount, legs.currency)}</span>
      </div>
    ) : (
      <div className={styles.legRow} />
    );

  return (
    <div className={styles.legs}>
      <div className={styles.legsHead}>Debit</div>
      <div className={styles.legsHead}>Credit</div>
      {Array.from({ length: rows }).map((_, i) => (
        <Fragment key={i}>
          <div>{cell(legs.debits[i])}</div>
          <div>{cell(legs.credits[i])}</div>
        </Fragment>
      ))}
      <div className={styles.legsFoot}>
        <div className={styles.legRow}>
          <span>Total DR</span>
          <span className={styles.legAmount}>{fmtAmount(debit, legs.currency)}</span>
        </div>
      </div>
      <div className={styles.legsFoot}>
        <div className={styles.legRow}>
          <span>Total CR</span>
          <span className={styles.legAmount}>{fmtAmount(credit, legs.currency)}</span>
        </div>
      </div>
      <div className={styles.balanced}>
        <StatusPill family={balanced ? "green" : "red"}>
          {balanced ? "Balanced — DR = CR" : "Out of balance"}
        </StatusPill>
      </div>
    </div>
  );
}

/**
 * Stage 8 — Doina's L646-653 five-row tie-out dashboard. Each row is one leg of the three-way
 * reconciliation: payment order / rail confirmation / subledger / settlement account / GL, each
 * with an amount and ✓/✗, then => RECONCILED.
 *
 * The rows are derived from `trace.reconciliation.legs` (the ledger's three-leg result) plus the
 * settlementPosition. Legs 1/2 are NOT_APPLICABLE for a book transfer (no rail, no settlement
 * run) and render "N/A — book transfer" rather than a tick. A PENDING leg (the settlement
 * journal has not posted yet) renders "awaiting the GL batch". The whole thing falls back to a
 * single "not yet checked" line when the reconciliation block is absent (a pre-stage-8 payment).
 */
function ReconciliationTieOut({ data, payment }) {
  const check = data?.check;
  const position = data?.position;

  if (!check) {
    return (
      <Body className={styles.muted}>
        Reconciliation has not run yet. It fires in the ledger service after the GL batch posts
        the settlement journal.
      </Body>
    );
  }

  const legByType = (type) => (check.legs || []).find((l) => l.leg === type);
  const leg1 = legByType("PAYMENT_RAIL");
  const leg2 = legByType("RAIL_SETTLEMENT");
  const leg3 = legByType("SETTLEMENT_GL");

  const minors = (v) => (v == null ? null : Number(v) / 100);
  const currency = payment?.currency || "USD";

  // The five rows Doina drew (L648-652). Each carries the amount that row represents and the
  // verdict for the leg that proves it.
  const rows = [
    {
      label: "Payment order",
      amount: payment?.amount,
      leg: leg1,
      naText: "N/A — book transfer",
    },
    {
      label: "Rail confirmation",
      amount: payment?.amount, // leg1 compares instruction == execution; the rail amount equals the instruction when MATCH
      leg: leg1,
      naText: "N/A — book transfer",
    },
    {
      label: "Subledger",
      amount: minors(leg3?.leftAmount ?? leg2?.rightAmount),
      leg: leg3,
      naText: "N/A — book transfer",
    },
    {
      label: "Settlement account",
      amount: position?.grossAmount,
      leg: leg2,
      naText: "N/A — book transfer",
    },
    {
      label: "General ledger",
      amount: minors(leg3?.rightAmount),
      leg: leg3,
      naText: "N/A — book transfer",
    },
  ];

  const verdictVariant = (result) =>
    result === "MATCH" ? "green" : result === "MISMATCH" ? "red" : "blue";

  const verdictLabel = (result) =>
    result === "MATCH" ? "✓" : result === "MISMATCH" ? "✗" : result === "NOT_APPLICABLE" ? "N/A" : "…";

  const overall = check.overallResult;

  return (
    <div className={styles.reconTieOut}>
      <div className={styles.reconRows}>
        {rows.map((r) => {
          const result = r.leg?.result;
          const isNA = result === "NOT_APPLICABLE";
          const isPending = result === "PENDING";
          return (
            <div className={styles.reconRow} key={r.label}>
              <span className={styles.reconLabel}>{r.label}</span>
              <span className={styles.reconAmount}>
                {isNA || isPending
                  ? (isNA ? r.naText : "awaiting the GL batch")
                  : (r.amount != null ? fmtAmount(r.amount, currency) : "—")}
              </span>
              <StatusPill family={verdictVariant(result)}>{verdictLabel(result)}</StatusPill>
            </div>
          );
        })}
      </div>
      <div className={styles.reconVerdict}>
        <StatusPill family={overall === "RECONCILED" ? "green" : overall === "DISCREPANT" ? "red" : "blue"}>
          {overall === "RECONCILED" ? "=> RECONCILED" : overall === "DISCREPANT" ? "DISCREPANT — discrepancy flagged" : "=> pending the GL batch"}
        </StatusPill>
      </div>
      {leg1?.detail && (
        <Body className={styles.muted}>{leg1.detail}</Body>
      )}
      {leg2?.detail && leg2.result !== "NOT_APPLICABLE" && (
        <Body className={styles.muted}>{leg2.detail}</Body>
      )}
      {leg3?.detail && (
        <Body className={styles.muted}>{leg3.detail}</Body>
      )}
    </div>
  );
}

function StateEvents({ events }) {
  if (!events?.length) return <Body className={styles.muted}>No transitions recorded.</Body>;
  return (
    <div>
      {events.map((e, i) => (
        <div className={styles.event} key={`${e.state}-${e.at}-${i}`}>
          <div>
            <StatusPill status={e.state} />{" "}
            <span className={styles.eventReason}>
              {e.reason || "—"}
              {e.actor ? ` · ${e.actor}` : ""}
              {e.actorType ? ` (${e.actorType})` : ""}
            </span>
          </div>
          <div className={styles.eventWhen}>{fmtWhen(e.at)}</div>
        </div>
      ))}
    </div>
  );
}

// `payments.checks[]`, written from stage 2 on: {stage, name, result, mode, detail, at,
// actor}. `outcome`/`reason` are read as fallbacks because this panel was built before the
// array existed and guessed those two names — a document written by the code carries
// `result`/`detail`.
/**
 * Render the per-check audit trail as a progressive gate flow rather than a flat list.
 *
 * The six stage-2 checks advance in the order a payment actually does — identity, then the
 * funding account, then entitlement, then the amount limit, then approval. Each phase is a
 * small headed group with the checks that belong to it, so the reader sees the gate sequence
 * rather than a shuffled list. Checks from later stages (which share `checks[]`) still render,
 * grouped under a generic heading. One row = a result pill + a concise plain-language label +
 * the backend's one-line detail.
 */
const CHECK_PHASE = {
  customer_authenticated: "Identity",
  account_active: "Account",
  account_unrestricted: "Account",
  customer_entitled: "Entitlement",
  payment_limit_available: "Payment limit",
  dual_approval: "Approval",
};
const CHECK_ORDER = ["Identity", "Account", "Entitlement", "Payment limit", "Approval"];
const CHECK_LABEL = {
  customer_authenticated: "Customer authenticated",
  account_active: "Account active",
  account_unrestricted: "No debit restriction",
  customer_entitled: "User entitled to debit",
  payment_limit_available: "Payment limit available",
  dual_approval: "Required approval",
};
const humanizeLabel = (s) =>
  (s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

function CheckRow({ c }) {
  const result = c.result || c.outcome;
  const detail = c.detail || c.reason;
  const name = CHECK_LABEL[c.name] || humanizeLabel(c.name) || "check";
  // The full detail is a hover tooltip, not truncated inline text. The row stays a compact
  // pill + name + mode so a long reason no longer pushes the check column open or ellipsizes.
  const row = (
    <div className={styles.checkRow}>
      <StatusPill family={checkPillFamily(result)}>{result || "—"}</StatusPill>
      <span className={styles.checkName}>{name}</span>
      {c.mode && <span className={styles.checkMode}>{c.mode}</span>}
    </div>
  );
  if (!detail) return row;
  return (
    <Tooltip trigger={row}>
      <span className={styles.checkDetailTip}>{detail}</span>
    </Tooltip>
  );
}

/**
 * Roll `checks[]` up to a one-line verdict — "does the top-level status say all good?"
 * Any FAIL dominates; then WARN, then PENDING, then SKIP; a clean PASS list reads "all
 * passed". The per-check trail stays one click away behind this summary.
 */
function summarizeChecks(checks) {
  const counts = { PASS: 0, FAIL: 0, WARN: 0, SKIP: 0, PENDING: 0 };
  checks.forEach((c) => {
    const r = (c.result || c.outcome || "").toUpperCase();
    if (r in counts) counts[r] += 1;
  });
  const { PASS, FAIL, WARN, SKIP, PENDING } = counts;
  let verdict;
  let family;
  if (FAIL) {
    verdict = `${FAIL} failed`;
    family = "red";
  } else if (WARN) {
    verdict = `${WARN} warning${WARN === 1 ? "" : "s"}`;
    family = "yellow";
  } else if (PENDING) {
    verdict = `${PENDING} pending`;
    family = "yellow";
  } else if (SKIP) {
    verdict = "passed";
    family = "green";
  } else {
    verdict = "all passed";
    family = "green";
  }
  const bits = [];
  if (PASS) bits.push(`${PASS} pass`);
  if (WARN) bits.push(`${WARN} warn`);
  if (SKIP) bits.push(`${SKIP} skip`);
  if (PENDING) bits.push(`${PENDING} pending`);
  if (FAIL) bits.push(`${FAIL} fail`);
  return { verdict, family, breakdown: bits.join(" · ") };
}

/**
 * The per-check audit trail, collapsed by default to a summary banner — the top-level
 * "is this stage clean?" answer — with the full trail behind a click. Shared by the
 * stages that append to `checks[]` (2/3/4/5), so the collapse/expand behaves the same
 * everywhere and one payment's checks never spill across a stage panel.
 *
 * `defaultOpen` lets a caller pin a specific stage's trail open (stage 2's gate flow is
 * the demo's telling screen); otherwise it starts closed.
 */
function Checks({ checks, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  if (!checks?.length) {
    return (
      <Body className={styles.muted}>
        No checks recorded for this payment.
      </Body>
    );
  }
  const summary = summarizeChecks(checks);
  const groups = CHECK_ORDER.map((phase) => ({
    phase,
    entries: checks.filter((c) => CHECK_PHASE[c.name] === phase),
  })).filter((g) => g.entries.length > 0);
  const general = checks.filter((c) => !CHECK_PHASE[c.name]);

  return (
    <div>
      <button
        type="button"
        className={styles.checkSummary}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <StatusPill family={summary.family}>{summary.verdict}</StatusPill>
        <span className={styles.checkSummaryCount}>
          {checks.length} check{checks.length === 1 ? "" : "s"}
        </span>
        {summary.breakdown && (
          <span className={styles.checkSummaryBreakdown}>{summary.breakdown}</span>
        )}
        <span className={styles.checkSummaryChevron} aria-hidden>
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <div className={styles.checkDetail}>
          <div className={styles.checkFlow}>
            {groups.map((g) => (
              <div className={styles.checkPhase} key={g.phase}>
                <div className={styles.checkPhaseLabel}>{g.phase}</div>
                {g.entries.map((c, i) => (
                  <CheckRow key={`${c.name || c.checkId}-${g.phase}-${i}`} c={c} />
                ))}
              </div>
            ))}
            {general.length > 0 && (
              <div className={styles.checkPhase}>
                <div className={styles.checkPhaseLabel}>Checks</div>
                {general.map((c, i) => (
                  <CheckRow key={`${c.name || c.checkId}-${i}`} c={c} />
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Stage 5's BUSINESS VIEW / ISO VIEW pair — her L548-565, the highest-value screen in the
 * demo (doc 16 §5).
 *
 * Her acceptance, verbatim:
 *   "The demo can show two tabs: BUSINESS VIEW - Payment ID: PAY-100023 - ABC Manufacturing
 *    -> Supplier XYZ - Amount: $25,000. ISO VIEW (Mapping to ISO 20022 -> pacs.008) -
 *    GrpHdr, CdtTrfTxInf, Dbtr, DbtrAcct, CdtrAgt, Cdtr, CdtrAcct, RmtInf, ...
 *    This demonstrates the relationship between business-domain data and payment messaging."
 *
 * That last line is the requirement, not a flourish: the ISO view lists each element **beside
 * the canonical field it was mapped from**, so a viewer can see the relationship rather than
 * being told about it. The mapper is a pure projection precisely so this claim holds
 * (`test_every_iso_value_comes_from_the_canonical_payment`).
 *
 * A book transfer has neither document — it reaches no rail boundary (doc 19 B4) — so the
 * component says so instead of rendering two empty tabs. An empty tab reads as broken; a
 * sentence reads as a design decision, which is what it is.
 */
function RailViews({ data }) {
  const business = data?.business;
  const iso = data?.iso;
  const xml = data?.xml;

  if (!iso) {
    return (
      <Body className={styles.muted}>
        No rail message: this payment settled on Leafy Bank&apos;s own books, so it crossed no
        rail boundary and no ISO 20022 message was generated.
      </Body>
    );
  }

  const businessRows = [
    ["Payment ID", business?.paymentId],
    [
      "Parties",
      business?.debtorName && business?.creditorName
        ? `${business.debtorName} \u2192 ${business.creditorName}`
        : null,
    ],
    ["Amount", fmtAmount(business?.amount, business?.currency)],
    ["Rail / network", [business?.rail, business?.clearingNetwork].filter(Boolean).join(" \u00b7 ")],
    ["End-to-end ID", business?.endToEndId],
    ["UETR", business?.uetr],
    ["Charge bearer", business?.chargeBearer],
    ["Creditor bank", [business?.creditorBankName, business?.creditorBankCountry].filter(Boolean).join(", ")],
    ["Creditor BIC", business?.creditorBic],
    ["Purpose", business?.purposeCode],
    ["Remittance", business?.remittanceInfo],
  ];

  // One row per produced element, paired with the canonical field it came from. Built from
  // the message itself rather than a hardcoded list, so an element added to the mapper shows
  // up here without editing this component.
  // Through the ISO envelope: a pacs.008 is `Document/FIToFICstmrCdtTrf/{GrpHdr,
  // CdtTrfTxInf}`, and `CdtTrfTxInf` is 1..n. Paths are shown relative to
  // `FIToFICstmrCdtTrf` — repeating the wrapper on every row would be noise.
  const isoBody = iso?.Document?.FIToFICstmrCdtTrf ?? iso;
  const isoRows = [];
  Object.entries(isoBody).forEach(([group, children]) => {
    (Array.isArray(children) ? children : [children]).forEach((child) => {
      Object.entries(child || {}).forEach(([name, value]) => {
        if (value == null) return;
        isoRows.push({
          element: `${group}/${name}`,
          value: typeof value === "object" ? JSON.stringify(value) : String(value),
        });
      });
    });
  });

  return (
    <Tabs aria-label="Payment views" setSelected={() => {}}>
      <Tab name="BUSINESS VIEW">
        <div className={styles.tabBody}>
          <KeyValues rows={businessRows} />
        </div>
      </Tab>
      <Tab name="ISO VIEW">
        <div className={styles.tabBody}>
          <Body className={styles.muted}>
            Mapping to ISO 20022 &rarr; {data?.execution?.messageFormat}
            {data?.mappingVersion ? ` (mapping ${data.mappingVersion})` : ""}
            {data?.simulated ? " \u00b7 SIMULATED rail" : ""}
          </Body>
          <div className={styles.isoRows}>
            {isoRows.map((row) => (
              <div key={row.element} className={styles.isoRow}>
                <span className={styles.isoElement}>{row.element}</span>
                <span className={styles.isoValue}>{row.value}</span>
              </div>
            ))}
          </div>
          <div className={styles.detailBlockTitle}>Message as sent</div>
          <div className={styles.codeScroll}>
            <Code language="json" copyButtonAppearance="hover">
              {JSON.stringify(iso, null, 2)}
            </Code>
          </div>
        </div>
      </Tab>
      {/* The same message serialised to real pacs.008 XML — stdlib ElementTree on the
          backend, derived at read time rather than stored (it is a rendering of the stored
          JSON, not a second copy of it).

          ⚠️ Well-formed and correctly namespaced, but NOT XSD-validated: no ISO 20022
          schema exists in this workspace. Do not claim conformance in front of an audience
          that would know the difference — claim the mapping, which is what her L565 asks
          for. */}
      {xml && (
        <Tab name="XML">
          <div className={styles.tabBody}>
            <Body className={styles.muted}>
              ISO 20022 {data?.execution?.messageFormat} as submitted to the{" "}
              {data?.execution?.clearingNetwork || "rail"} (SIMULATED). Well-formed and
              namespaced; not schema-validated.
            </Body>
            <div className={styles.codeScroll}>
              <Code language="xml" copyButtonAppearance="hover">
                {xml}
              </Code>
            </div>
          </div>
        </Tab>
      )}
    </Tabs>
  );
}


/** Per-kind key/value rows. One place to extend when a later stage lands. */
// The FR-3.7 corridor category (`validation.determinedCategory`) is the finer domestic-vs-
// cross-border determination; `wireType` (DOMESTIC/INTERNATIONAL) is the coarser stage-1 field,
// used as the fallback when the corridor hasn't been determined (e.g. a pre-stage-3 payment).
const CORRIDOR_LABELS = {
  "domestic-same-bank": "Domestic — same bank",
  "domestic-different-bank": "Domestic — different bank",
  "cross-border": "Cross-border",
};
const corridorLabel = (payment) =>
  CORRIDOR_LABELS[payment?.validation?.determinedCategory] ?? payment?.wireDetails?.wireType;

function summaryRows(stage, payment) {
  const d = stage.data;
  switch (stage.kind) {
    case "initiation":
      // The canonical instruction. The parties have their own "Immutable parties" block
      // below, so they are deliberately not repeated here.
      return [
        ["Type · Rail", `${d?.type || "—"} · ${d?.rail || "—"}`],
        ["Amount", fmtAmount(d?.amount, d?.currency)],
        ["Priority", d?.priority],
        ["Charge bearer", d?.chargeBearer],
        ["Channel", d?.initiation?.channel],
        ["Customer", d?.customerId],
        ["Requested execution date", d?.requestedExecutionDate],
        ["Purpose", d?.remittance?.unstructured],
        ["End-to-end reference", d?.remittance?.reference],
        ["Client reference", d?.remittance?.invoiceNo],
        ["Initiated", fmtWhen(d?.initiatedAt)],
      ];
    case "checks": {
      // Stage 2's two summary blocks. The authentication assessment says what the CHANNEL
      // asserted — `NONE` means nothing was asserted, which is why the check beneath reads
      // SKIP rather than PASS.
      const auth = payment?.authentication;
      const ent = payment?.entitlement;
      return [
        ["Checks recorded", d?.length],
        ["Authentication", auth ? `${auth.method}${auth.factorCount ? ` · ${auth.factorCount} factor(s)` : ""}` : null],
        ["Session", auth?.sessionRef],
        ["Segment", ent?.segment],
        ["Signing rule", ent?.signingRule],
        ["Per-payment entitlement", ent?.perPaymentLimit != null ? fmtAmount(ent.perPaymentLimit, payment?.currency) : null],
        ["Dual approval", ent ? (ent.dualApprovalRequired ? `Required · ${ent.dualApprovalBy}${ent.dualApprovalSimulated ? " (simulated)" : ""}` : "Not required") : null],
        ["Assessed", fmtWhen(ent?.assessedAt)],
      ];
    }
    case "enrichment": {
      // What the stage concluded, above the field-level diff. `checks` spans all three of
      // stage 3's halves (`3 validate`, `3 enrich`, `3 final-validate`).
      const e = d?.enrichment;
      const checks = d?.checks || [];
      const warned = checks.filter((c) => c.result === "WARN").length;
      const failed = checks.filter((c) => c.result === "FAIL").length;
      return [
        ["Checks recorded", checks.length || null],
        ["Warnings", warned || null],
        ["Refusals", failed || null],
        ["Fields enriched", e?.resolved?.length ?? null],
        ["Corridor", corridorLabel(payment)],
        ["Purpose", payment?.categoryPurpose],
        ["Charges", payment?.fees?.length
          ? payment.fees.map((f) => `${fmtAmount(f.amount, f.currency)} ${f.type} (${f.chargedTo})`).join(", ")
          : null],
        ["FX rate", payment?.fxRate ?? null],
        ["Enriched at", fmtWhen(e?.resolvedAt)],
      ];
    }
    case "authorization": {
      // Doina's L508-515 display, as a summary: the routing decision above, the risk
      // decision below. `checks` carries her four display lines verbatim (the backend names
      // them to match), so this block deliberately does NOT restate them — it gives the
      // numbers and identifiers the checks refer to.
      const f = d?.fraud;
      const s = d?.sanctions;
      const checks = d?.checks || [];
      const warned = checks.filter((c) => c.result === "WARN").length;
      const failed = checks.filter((c) => c.result === "FAIL").length;
      return [
        ["Checks recorded", checks.length || null],
        ["Warnings", warned || null],
        ["Refusals", failed || null],
        ["Clearing network", d?.network],
        ["Fraud score", f?.score != null ? `${f.score}/100` : null],
        ["Decision", f?.decision],
        ["Rules fired", f?.rulesFired?.length ? f.rulesFired.join(", ") : (f ? "none" : null)],
        ["Sanctions", s?.status ? `${s.status} · ${s.provider || "—"}` : null],
        ["Alert ID", f?.alertId],
        ["Routing snapshot", d?.refs?.routingSnapshotId],
        ["Payment order", d?.refs?.paymentOrderId],
        ["Authorised", fmtWhen(payment?.clearing?.authorisedAt)],
        ["Assessed", fmtWhen(f?.checkedAt)],
      ];
    }
    case "railExecution": {
      // Her L548-553 three lines live in the BUSINESS VIEW tab; this block gives what an
      // operator needs *about the execution* — which attempt, which network, what the rail
      // said back, and the two artifact ids.
      const e = d?.execution;
      const checks = d?.checks || [];
      return [
        ["Checks recorded", checks.length || null],
        ["Attempt", e ? `${e.attempt} of ${d?.attempts?.length || 1}` : null],
        ["Rail / network", [payment?.rail, e?.clearingNetwork].filter(Boolean).join(" · ") || payment?.rail],
        ["Message", e ? `${e.messageStandard} ${e.messageFormat}` : "none — book transfer"],
        ["Execution status", e?.status],
        ["Rail status", d?.railStatus?.code ? `${d.railStatus.code} — ${d.railStatus.reason || ""}` : null],
        ["Network ref", d?.clearing?.networkRef],
        ["Network code", d?.clearing?.networkCode],
        ["Settlement date", d?.clearing?.settlementDate],
        ["Payment execution", e?.paymentExecutionId],
        ["Payment message", e?.paymentMessageId],
        ["Submitted", fmtWhen(d?.clearing?.submittedAt)],
        ["Acknowledged", fmtWhen(e?.acknowledgedAt)],
        ["Simulated rail", e ? (e.simulated ? "Yes — no external network is contacted" : "No") : null],
      ];
    }
    case "transaction":
      return [
        ["Bank ref", d?.bankRef],
        ["Direction", d?.direction],
        ["Amount", fmtAmount(d?.amount, d?.currency)],
        ["Balance after", d?.balanceAfter != null ? fmtAmount(d.balanceAfter, d?.currency) : null],
        ["From", d?.payer ? `${d.payer.name || "—"} (${d.payer.accountId || "—"})` : null],
        ["To", d?.payee ? `${d.payee.name || "—"} (${d.payee.accountId || "—"})` : null],
        ["Value date", d?.valueDate],
        ["Created", fmtWhen(d?.createdAt)],
      ];
    case "ledgerEvent":
      return [
        ["Event ID", d?.eventId],
        ["Event type", d?.eventType],
        ["Posting mode", d?.postingMode?.type],
        ["Journal entry", d?.postingResult?.journalEntryId],
        ["Occurred", fmtWhen(d?.occurredAt)],
        ["Posted", d?.postingResult?.postedAt ? fmtWhen(d.postingResult.postedAt) : null],
      ];
    case "subLedger": {
      const first = d?.[0];
      return [
        ["Entries", d?.length],
        ["Journal entry", first?.journalEntryId || "—"],
        ["Period", first?.periodCode],
        ["Posting date", fmtWhen(first?.postingDate)],
      ];
    }
    case "journal":
      return [
        ["Journal ID", d?.journalId],
        ["Journal type", d?.journalType],
        ["Period", d?.periodCode],
        ["Txn count", d?.sourceReference?.txnCount],
        ["Created by", d?.createdBy],
        ["Created", fmtWhen(d?.createdAt)],
      ];
    case "reconciliation": {
      // Stage 8 — the three-way tie-out summary. The five-row dashboard itself renders in
      // ReconciliationTieOut (below); these rows are the supporting facts: the reconciliation
      // item, the settlement position, the journal that proved leg 3, and when it ran.
      const check = d?.check;
      const pos = d?.position;
      const overall = check?.overallResult;
      return [
        ["Overall", overall ? overall : "not yet checked"],
        ["Reconciliation item", check?.legs ? payment?.refs?.reconciliationItemId : null],
        ["Settlement position", pos?.settlementPositionId || payment?.refs?.settlementPositionId],
        ["Settlement model", pos?.modelLabel],
        ["Journal (leg 3)", check?.journalEntryId],
        ["Checked", check?.checkedAt ? fmtWhen(check.checkedAt) : null],
      ];
    }
    default:
      return [["Current state", payment?.lifecycle?.currentState]];
  }
}

/** One immutable party snapshot (debtor or creditor) as a card of key/values. */
function PartyCard({ label, party, external }) {
  const rows = [
    ["Name", party?.name],
    ["Account ID", party?.accountId],
    ["Account no", party?.accountNo ? `····${String(party.accountNo).slice(-4)}` : null],
    ["Account type", party?.accountType],
    ["Bank", party?.bankName],
    ["BIC", party?.bic],
    ["Country", party?.bankCountry],
  ].filter(([, v]) => v != null);
  return (
    <div className={styles.partyCard}>
      <div className={styles.partyLabel}>
        {label}
        {external && !party?.accountId && (
          <span className={styles.partyExternal}>external</span>
        )}
      </div>
      <KeyValues rows={rows.length ? rows : [["—", "—"]]} />
    </div>
  );
}

/**
 * The rail-specific initiation envelope — Doina's "type-specific initiation envelope". One is
 * populated per rail; the other two stay all-null. Shows the fields stage 1 genuinely knows,
 * and says what a later stage resolves (so nulls read as "not yet", not "missing").
 */
function InitEnvelope({ payment }) {
  const rail = payment?.rail;
  let rows = null;
  let note = null;
  if (rail === "WIRE") {
    const w = payment?.wireDetails || {};
    rows = [
      ["Message definition", w.messageDefinitionIdentifier],
      ["Payment method", w.paymentMethod],
      ["Wire type", w.wireType],
      ["Payment info id", w.paymentInformationId],
      ["Service level", w.paymentTypeInformation?.serviceLevel?.code],
      ["Initiating party", w.initiatingParty?.name],
      // Phase 1 has no on-behalf-of scenario, so these are null (doc 17 §6). Render an explicit
      // "—" (N/A) rather than dropping the row, so an audience can see the fields exist.
      ["Ultimate debtor", w.ultimateDebtor?.name ?? "—"],
      ["Ultimate creditor", w.ultimateCreditor?.name ?? "—"],
    ];
    note =
      "Wire type is derived from the two bank countries. Routing fields (network, local " +
      "instrument code) stay null here — they are resolved in stage 4 orchestration.";
  } else if (rail === "INTERNAL") {
    const i = payment?.internalDetails || {};
    rows = [
      ["Transfer type", i.transferType],
      ["Posting reference", i.postingReference],
    ];
    note =
      "postingReference points at the ledger event the ledger service writes asynchronously — " +
      "null at initiation.";
  }
  return (
    <div>
      <KeyValues rows={rows ? rows.filter(([, v]) => v != null) : [["Rail", rail || "—"]]} />
      {note && <div className={styles.stageNote}>{note}</div>}
      {rail && (
        <div className={styles.stageNote}>
          Exactly one envelope is populated per rail — the {rail} one carries the fields above;
          the other two stay all-null.
        </div>
      )}
    </div>
  );
}

/** A titled card of key/values — reused for the stage-2 authentication/entitlement gates. */
function DetailCard({ label, tag, rows }) {
  return (
    <div className={styles.partyCard}>
      <div className={styles.partyLabel}>
        {label}
        {tag && <span className={styles.partyExternal}>{tag}</span>}
      </div>
      <KeyValues rows={rows.length ? rows : [["—", "—"]]} />
    </div>
  );
}

function StageDetailBody({ stage, payment, onApprove }) {
  // Raw JSON is behind a toggle so it never buries the informative blocks below. The hook
  // must sit above the early returns (rules of hooks).
  const [showRaw, setShowRaw] = useState(false);

  if (!stage) return null;

  if (!stage.reached) {
    return (
      <Body className={styles.muted}>
        {stage.label} — not reached yet.
      </Body>
    );
  }

  const showLegs = !!stage.legs;
  const showInitiation = stage.kind === "initiation";
  const showEnrichment = stage.kind === "enrichment";
  const showAuthorization = stage.kind === "authorization";
  const showRailExecution = stage.kind === "railExecution";
  const showReconciliation = stage.kind === "reconciliation";
  // Stages 3 and 4 render checks too, but their `data` is an object rather than the bare
  // array stage 2 passes, so the shapes are resolved separately.
  const showChecks =
    stage.kind === "checks" || showEnrichment || showAuthorization || showRailExecution;
  const showStates = stage.kind === "states";
  const checkList =
    showEnrichment || showAuthorization || showRailExecution
      ? stage.data?.checks
      : stage.data;
  // Stage 1's only lifecycle event is INITIATED — the moment the instruction was captured.
  const initEvents = (payment?.lifecycle?.events || []).filter(
    (e) => (e.state || "").toUpperCase() === "INITIATED"
  );
  // The raw document is always present as an expandable artifact below the structured
  // blocks. Most stages dump their `data`; stage 2 is a checks stage (so `data` is the
  // checks array) and instead carries an explicit `raw` object of the assessments.
  const showRawToggle =
    (!!stage.data && !showChecks && !showStates && !showEnrichment) || !!stage.raw;
  // Stage 2 is the only `checks`-kind stage: it renders the two gate assessments instead
  // of the generic Summary, and surfaces the dual-approval rule (Doina's $25k → $10k demo).
  const showStageTwo = stage.kind === "checks";
  const a = payment?.authentication;
  const e = payment?.entitlement;
  const authRows = [
    ["Method", a?.method],
    ["Factor count", a?.factorCount],
    ["Caller type", a?.callerType],
    ["Session", a?.sessionRef],
    ["Step-up", a?.stepUp ? "Yes" : null],
    ["Sufficient", a?.sufficient ? "Yes" : "No"],
    ["Assessed at", fmtWhen(a?.assessedAt)],
  ];
  const entRows = [
    ["Segment", e?.segment],
    ["Signing rule", e?.signingRule],
    ["Per-payment limit", e?.perPaymentLimit != null ? fmtAmount(e.perPaymentLimit, payment?.currency) : null],
    ["Dual-approval threshold", e?.dualApprovalThreshold != null ? fmtAmount(e.dualApprovalThreshold, payment?.currency) : null],
    ["Dual approval required", e?.dualApprovalRequired ? `Yes · ${e.dualApprovalBy || "—"}` : "No"],
    ["Assessed at", fmtWhen(e?.assessedAt)],
  ];

  return (
    <>
      {stage.intro && (
        <div className={styles.stageIntro}>
          <div className={styles.stageIntroLabel}>What this stage does</div>
          <div className={styles.stageIntroText}>{stage.intro}</div>
        </div>
      )}
      <div className={styles.detailColumns}>
        {showStageTwo && (
          <>
            {stage.actionRequired && (
              <div className={styles.stepUpCallout}>
                <div className={styles.stepUpCalloutTitle}>
                  <Icon glyph="Lock" />
                  <span>Verification required here</span>
                </div>
                <Body>
                  This payment is above the account&apos;s step-up threshold and is waiting for an
                  additional authentication factor before it can proceed. Approve it here to
                  continue it through the lifecycle.
                </Body>
                <Button variant="primary" onClick={onApprove}>
                  Authenticate &amp; continue
                </Button>
              </div>
            )}
            <div className={styles.stageTwoGrid}>
            <div className={styles.detailBlock}>
              <div className={styles.detailBlockTitle}>The two gates</div>
              <div className={styles.partyGrid}>
                <DetailCard label="Party authentication" rows={authRows} />
                <DetailCard label="Payment entitlement" rows={entRows} />
              </div>
              {payment?.amount != null &&
                e?.dualApprovalThreshold != null &&
                Number(payment.amount) > Number(e.dualApprovalThreshold) && (
                <div className={styles.dualApprovalCallout}>
                  <strong>Dual approval applies:</strong>{" "}
                  {fmtAmount(payment?.amount, payment?.currency)} is above the{" "}
                  {e?.segment || "segment"} dual-approval threshold of{" "}
                  {fmtAmount(e?.dualApprovalThreshold, payment?.currency)} — a second approver
                  ({e?.dualApprovalBy || "—"}) is required. Simulated: no human approved this
                  payment; the interactive approval queue is deferred.
                </div>
              )}
            </div>
            <div className={styles.detailBlock}>
              <div className={styles.detailBlockTitle}>Checks — gate results</div>
              <Checks checks={checkList} />
            </div>
            </div>
          </>
        )}

        {/* Stage 2 renders its own checks inside the two-column grid; every other checks
            stage (3/4/5) uses this generic block. */}
        {showChecks && !showStageTwo && !showEnrichment && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>Checks</div>
            <Checks checks={checkList} />
          </div>
        )}

        {showStates && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>State transitions</div>
            <StateEvents events={stage.data} />
          </div>
        )}

        {/* Stage 2 shows both: the six check results, and the assessment blocks
            (`authentication{}` / `entitlement{}`) that say what they were judged against. */}
        {showInitiation && initEvents.length > 0 && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>State transition</div>
            <StateEvents events={initEvents} />
          </div>
        )}

        {!showStates && !showStageTwo && !showEnrichment && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>Summary</div>
            <KeyValues rows={summaryRows(stage, payment)} />
          </div>
        )}

        {showInitiation && (
          <div className={styles.detailBlockWide}>
            <div className={styles.detailBlockTitle}>Immutable parties</div>
            <div className={styles.partyGrid}>
              <PartyCard label="Debtor — the payer" party={stage.data?.debtor} />
              <PartyCard
                label="Creditor — the beneficiary"
                party={stage.data?.creditor}
                external
              />
            </div>
          </div>
        )}

        {showInitiation && (
          <div className={styles.detailBlockWide}>
            <div className={styles.detailBlockTitle}>Initiation envelope</div>
            <InitEnvelope payment={stage.data} />
          </div>
        )}

        {showReconciliation && (
          <div className={styles.detailBlockWide}>
            <div className={styles.detailBlockTitle}>
              Three-way match: payment to rail, rail to settlement account, settlement account to GL
            </div>
            <ReconciliationTieOut data={stage.data} payment={payment} />
          </div>
        )}

        {showReconciliation && !!stage.data?.events?.length && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>State transitions</div>
            <StateEvents events={stage.data.events} />
          </div>
        )}

        {showEnrichment && (
          <EnrichmentBody stage={stage} payment={payment} checkList={checkList} />
        )}

        {showRailExecution && (
          <div className={styles.detailBlockWide}>
            <div className={styles.detailBlockTitle}>
              Canonical payment &rarr; ISO 20022
            </div>
            <RailViews data={stage.data} />
          </div>
        )}

        {(showAuthorization || showRailExecution) && !!stage.data?.events?.length && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>State transitions</div>
            <StateEvents events={stage.data.events} />
          </div>
        )}

        {showLegs && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>Double-entry</div>
            <Legs legs={stage.legs} />
          </div>
        )}

        {showRawToggle && (
          <div className={styles.detailBlockWide}>
            <div className={styles.rawToggleBar}>
              <button
                type="button"
                className={styles.rawToggle}
                onClick={() => setShowRaw((v) => !v)}
                aria-expanded={showRaw}
              >
                <span className={styles.rawToggleIco}>{"{ }"}</span>
                {showRaw ? "Hide raw document" : "Show raw document"}
              </button>
            </div>
            {showRaw && (
              <div className={styles.codeScroll} style={{ marginTop: 8 }}>
                <Code language="json" copyButtonAppearance="hover">
                  {JSON.stringify(stage.raw || stage.data, null, 2)}
                </Code>
              </div>
            )}
          </div>
        )}
      </div>
    </>
  );
}

/**
 * The three independent fact axes — posting, settlement, reconciliation — that advance
 * alongside `currentState` but not in lockstep with it (research §1.4). A journal's legs
 * can post at different times than the payment settles, so these cannot be folded into the
 * linear timeline. Surfaced as a first-class row under the header, separate from the rail.
 * Null = the axis has not started; renders a neutral "not started" pill.
 */
function AxesRow({ payment }) {
  const lc = payment?.lifecycle;
  const axes = [
    { label: "Posting", value: lc?.postingStatus },
    { label: "Settlement", value: lc?.settlementStatus },
    { label: "Reconciliation", value: lc?.reconciliationStatus },
  ];
  return (
    <div className={styles.axesRow}>
      <span className={styles.axesHeading}>Independent axes</span>
      {axes.map((a) => (
        <div className={styles.axisItem} key={a.label}>
          <span className={styles.axisLabel}>{a.label}</span>
          <StatusPill status={a.value} label={a.value || "not started"} />
        </div>
      ))}
    </div>
  );
}

export default function PaymentDeepDive({ paymentId, refreshKey, onBack }) {
  // 2026-09-09 (Kiran): the step-up approval happens HERE, at stage 2, not at initiate. A held
  // payment is resumed from this view; `nudge` bumps into the hook's refresh key so the
  // lifecycle re-renders once the resume advances it past INITIATED.
  const [nudge, setNudge] = useState(0);
  const [stepUpOpen, setStepUpOpen] = useState(false);
  const [stepUpError, setStepUpError] = useState(null);
  const { payment, loading, error } = usePaymentWorkflow(
    paymentId,
    (refreshKey || 0) + nudge
  );
  // Ledger half. Self-terminating poll — stops once the journal entry lands.
  const { trace } = usePipelineTrace(paymentId, !!paymentId);
  const [selectedKey, setSelectedKey] = useState("initiation");

  async function resumePayment() {
    if (!paymentId) return;
    // Close the modal as soon as the OTP is verified — the Resume API call that
    // follows is not the user's action and making them wait for it with the modal
    // still open reads as "nothing happened". If Resume fails, the error surfaces
    // in the panel body banner (stepUpError) below.
    setStepUpOpen(false);
    setStepUpError(null);
    const { data, error: err } = await coreApi("PaymentOrderProcedure/Resume", {
      method: "POST",
      body: { paymentId },
    });
    if (err) {
      setStepUpError(err);
      return;
    }
    setNudge((n) => n + 1);
  }
  const rowRefs = useRef({});
  const setRowRef = useCallback(
    (key) => (el) => {
      rowRefs.current[key] = el;
    },
    []
  );
  // Per-payment guard so the failed-stage auto-expand fires once, not on every trace re-poll.
  const autoExpandedFor = useRef(null);

  // Reset to the first stage when a different payment is opened — keyed on paymentId, not
  // on the trace, so the 2s re-polls don't clobber the current selection every tick.
  useEffect(() => {
    setSelectedKey("initiation");
    autoExpandedFor.current = null;
  }, [paymentId]);

  const stages = useMemo(
    () => (payment ? buildLifecycleStages(payment, trace) : null),
    [payment, trace]
  );
  const states = useMemo(
    () => nodeStates(stages, payment?.status),
    [stages, payment?.status]
  );

  // Research §3.4: a terminal failure auto-expands the failing stage so the red banner and
  // reason are visible without a click. Runs once per payment, after stages first arrive.
  useEffect(() => {
    if (!stages || autoExpandedFor.current === paymentId) return;
    const failedIdx = states.indexOf("failed");
    if (failedIdx >= 0) {
      setSelectedKey(stages[failedIdx].key);
      autoExpandedFor.current = paymentId;
    }
  }, [stages, states, paymentId]);

  // Clicking the mini-stepper or a row header scrolls the matching row into view.
  useEffect(() => {
    rowRefs.current[selectedKey]?.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
    });
  }, [selectedKey]);

  if (!paymentId) {
    return (
      <div className={styles.panel}>
        <div className={styles.emptyState}>
          Select a payment to trace it through the lifecycle.
        </div>
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      <div className={styles.panelHeader}>
        <div className={styles.panelTitleRow}>
          {onBack && (
            <Button
              size="xsmall"
              leftGlyph={<Icon glyph="ArrowLeft" />}
              onClick={onBack}
              className={styles.backButton}
            >
              Payments
            </Button>
          )}
          <span className={styles.panelTitle}>Payment lifecycle</span>
        </div>
        <div className={styles.panelHeadRight}>
          <span className={`${styles.mono} ${styles.muted}`}>{paymentId}</span>
          {payment?.status && (
            <StatusPill status={payment.status} />
          )}
        </div>
      </div>

      <StepUpModal
        open={stepUpOpen}
        reason={payment?.stepUpReason || "step-up authentication required"}
        onCancel={() => {
          setStepUpOpen(false);
          setStepUpError(null);
        }}
        onSuccess={resumePayment}
      />

      <div className={styles.panelBody}>
        {error && <Banner variant="danger">Could not load payment — {error}</Banner>}
        {stepUpError && <Banner variant="danger">{stepUpError}</Banner>}
        {loading && <div className={styles.emptyState}>Loading…</div>}
        {!loading && !error && !payment && (
          <div className={styles.emptyState}>Payment not found: {paymentId}</div>
        )}

        {stages && (
          <>
            <AxesRow payment={payment} />
            <MiniStepper
              stages={stages}
              states={states}
              selectedKey={selectedKey}
              onSelect={setSelectedKey}
            />
            <VerticalTimeline
              stages={stages}
              states={states}
              selectedKey={selectedKey}
              onSelect={setSelectedKey}
              payment={payment}
              setRowRef={setRowRef}
              onApprove={() => setStepUpOpen(true)}
            />
          </>
        )}
      </div>
    </div>
  );
}
