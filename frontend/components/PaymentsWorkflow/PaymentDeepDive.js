"use client";

// The payment lifecycle deep dive — a horizontal stage stepper (the only navigation) over one
// detail pane (StagePane) that shows the selected stage. A terminal failure or an open
// exception auto-selects the stage that owns it; the stepper marks stages needing attention.
//
// Two data sources, composed client-side and never server-side:
//   * /workflow/payments/{id}  (transactions) — stages 1-4
//   * /pipeline/trace/{id}     (ledger)       — stages 5-8
// Neither service reads the other's collections (decisions.md 2026-06-18).
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import Banner from "@leafygreen-ui/banner";
import Code from "@leafygreen-ui/code";
import { Tab, Tabs } from "@leafygreen-ui/tabs";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import Tooltip from "@leafygreen-ui/tooltip";
import TextInput from "@leafygreen-ui/text-input";
import { Select, Option } from "@leafygreen-ui/select";
import { Body } from "@leafygreen-ui/typography";

import styles from "./PaymentsWorkflow.module.css";
import StatusPill from "./StatusPill";
import LensToggle, { useLens } from "./LensToggle";
import MongoRail from "./MongoRail";
import KeyFacts from "./KeyFacts";
import {
  PostingChain, FundsFlow, CategoryTable, FxProvenance, CutoffClock, PartyFlow, EnvelopeChips,
  StatePath, IntakeOrder, BianStrip, Card, StageStates, GateCards, LimitGauge, ResolutionOutcome,
  RouteMap, FraudMeter, AcceptanceRollup, RailFlow, SettlementOutcomes, PostingDirection,
  InboundResolutionChecks, InboundValidationChecks,
} from "./StageVisuals";
import { stageBian, stageCopy, stageFacts, stageWrites, settlementDelta } from "./stageContent";
import StepUpModal from "@/components/StepUpModal/StepUpModal";
import { buildLifecycleStages, groupLifecycleStages, legTotals } from "./lifecycleStages";
import { usePaymentWorkflow, usePipelineTrace, useBatchTick, useReconciliationAgent, useLinkCandidates } from "@/lib/api/hooks";
import { coreApi, agentApi, pipelineApi } from "@/lib/api/client";
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

// A completed stage gets a ✓ whether it is a linear-saga stage (1-5, where reaching a later
// stage proves the order) or an independent axis (6-8, where completion is the stage's OWN
// terminal fact). The two progress models differ in nodeStates, not in the glyph: an axis that
// settles or reconciles early shows a ✓ while the neighbouring axis is still in flight, which
// reads as "this axis is done" — not as a sequencing claim about the stage beside it.
const nodeGlyph = (state) =>
  state === "failed" ? "×" : state === "completed" ? "✓" : "";

/**
 * Per-stage node state for the stage stepper.
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

/** The things in a stage that need someone's attention, across a grouped stage's panels. */
function stageAttention(stage) {
  const panels = stage.children?.length > 1 ? stage.children : [stage];
  const openExceptions = panels
    .flatMap((p) => p.exceptions || [])
    .filter((e) => e?.status === "OPEN").length;
  return {
    openExceptions,
    stepUp: panels.some((p) => p.actionRequired),
    review: panels.some((p) => p.data?.reviewActionRequired),
  };
}

function attentionLabel({ openExceptions, stepUp, review }) {
  if (openExceptions) return `${openExceptions} open exception${openExceptions > 1 ? "s" : ""}`;
  if (stepUp) return "Waiting for step-up authentication";
  if (review) return "Waiting for manual review";
  return null;
}

/**
 * The horizontal stage selector — the only navigation in the lifecycle panel. Each node shows
 * its state, label and one line of meta; an orange dot marks a stage that needs attention so
 * the presenter sees where to click without opening anything. Linear-saga stages downstream
 * of a terminal failure are dimmed (they never ran); the independent axes (stage >= 6) are
 * parallel, not downstream, so they are not.
 */
function MiniStepper({ stages, states, selectedKey, onSelect }) {
  const failedIdx = states.indexOf("failed");

  // Left/right arrows move between stages (roving focus over the buttons).
  function onKeyDown(e) {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    const next = stages[stages.findIndex((s) => s.key === selectedKey) + step];
    if (!next) return;
    e.preventDefault();
    onSelect(next.key);
    e.currentTarget.querySelector(`[data-stage-key="${next.key}"]`)?.focus();
  }

  return (
    <div className={styles.miniStepperScroll}>
      <div className={styles.miniStepperTrack} onKeyDown={onKeyDown}>
        {stages.map((s, i) => {
          const issue = attentionLabel(stageAttention(s));
          const dimmed = failedIdx >= 0 && i > failedIdx && s.stage < 6;
          return (
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
                data-stage-key={s.key}
                className={`${styles.miniNode} ${
                  selectedKey === s.key ? styles.miniNodeActive : ""
                } ${dimmed ? styles.miniNodeDimmed : ""}`}
                onClick={() => onSelect(s.key)}
                aria-current={selectedKey === s.key ? "step" : undefined}
                title={issue ? `${s.label} — ${issue}` : s.label}
              >
                <span className={`${styles.miniCircle} ${styles[`mini${cap(states[i])}`]}`}>
                  {nodeGlyph(states[i])}
                  {issue && <span className={styles.miniIssueDot} aria-label={issue} />}
                </span>
                <span className={styles.miniLabel}>{s.label}</span>
                {s.meta && <span className={styles.miniMeta}>{s.meta}</span>}
              </button>
            </Fragment>
          );
        })}
      </div>
    </div>
  );
}

/**
 * What the operator must do next, lifted out of the stage body so it sits right under the
 * stage header. The handlers are the ones the bodies used before; only the placement moved.
 * Open exceptions keep their resolve buttons in the exceptions block, so they get a pointer,
 * not a second set of buttons.
 */
function NextAction({ attention, onApprove, onResolve }) {
  const { openExceptions, stepUp, review } = attention;
  return (
    <>
      {stepUp && (
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
      {review && onResolve && (
        <div className={styles.stepUpCallout}>
          <div className={styles.stepUpCalloutTitle}>
            <Icon glyph="Diagram3" />
            <span>Manual review required here</span>
          </div>
          <Body>
            Fraud scoring held this payment for manual review. Approve to commit the
            execution path and continue it through the lifecycle, or decline to reject it.
            No money has moved yet.
          </Body>
          <div className={styles.resolveActions}>
            <Button variant="primary" onClick={() => onResolve("APPROVED")}>
              Approve &amp; continue
            </Button>
            <Button variant="danger" onClick={() => onResolve("REJECTED")}>
              Decline
            </Button>
          </div>
        </div>
      )}
      {openExceptions > 0 && (
        <div className={styles.nextActionChip}>
          <Icon glyph="Warning" />
          <span>
            {openExceptions} open exception{openExceptions > 1 ? "s" : ""} on this stage —
            resolve {openExceptions > 1 ? "them" : "it"} in the Exceptions section below.
          </span>
        </div>
      )}
    </>
  );
}

/**
 * The single detail pane under the stepper: header (label, status, meta), failure banner,
 * next action, then the stage body. A grouped stage (6, 7) shows its panels as sub-tabs; the
 * group's exceptions render ONCE above the tabs, because the lifecycle model attaches them to
 * the first panel and a per-tab copy would put two live resolve buttons on one exception.
 */
function StagePane({ stage, state, payment, trace, lens, onApprove, onResolve, onResolveException, onResolveUta, onLedgerAction, onAcknowledgeAgent, refreshKey = 0 }) {
  const [panelIdx, setPanelIdx] = useState(0);
  // A different stage (or a payment with fewer panels) must not keep a stale tab index.
  useEffect(() => setPanelIdx(0), [stage?.key]);

  if (!stage) return null;

  const grouped = stage.children?.length > 1;
  const attention = stageAttention(stage);
  const actions = { onApprove, onResolve, onResolveException, onResolveUta, onLedgerAction, onAcknowledgeAgent };
  const claimer = grouped ? stage.children.find((c) => (c.exceptions || []).length) : null;
  const copy = stageCopy(stage, payment?.direction);
  const outcome = lens === "technical" ? copy?.technical : copy?.business;
  const writes = stageWrites(stage);
  const sources = { payment, trace };
  const bian = stageBian(stage, payment?.direction);
  const facts = stageFacts(stage, payment);
  const settlementPosition = stage.children?.find((c) => c.key === "settlementConfirm")?.data?.position;

  return (
    <section className={styles.stagePane} aria-live="polite">
      <div className={styles.stagePaneHead}>
        <span className={styles.stagePaneLabel}>{stage.label}</span>
        {stage.status && <StatusPill status={stage.status} />}
        {stage.meta && <span className={styles.stagePaneMeta}>{stage.meta}</span>}
      </div>

      {state === "failed" && (
        <div className={styles.failedBanner}>
          Payment stopped at {stage.label} — status {payment?.status || "unknown"}.
        </div>
      )}

      <BianStrip bian={bian} showOperation={lens !== "business"} />

      {outcome && <div className={styles.outcomeLine}>{outcome}</div>}

      {!stage.reached ? (
        <>
          {stage.intro && (
            <div className={styles.stageIntro}>
              <div className={styles.stageIntroLabel}>What this stage does</div>
              <div className={styles.stageIntroText}>{stage.intro}</div>
            </div>
          )}
          <Body className={styles.muted}>{stage.label} — not reached yet.</Body>
        </>
      ) : (
        <div className={`${styles.paneLayout} ${styles[`lens_${lens}`]}`}>
          <div className={styles.paneMain}>
          <NextAction attention={attention} onApprove={onApprove} onResolve={onResolve} />
          <KeyFacts facts={facts} />
          {stage.key === "validation" && (
            <>
              <CategoryTable payment={payment} />
              <FxProvenance payment={payment} />
            </>
          )}
          {stage.key === "authorization" && <CutoffClock snapshot={stage.data?.routingSnapshot} />}
          {stage.key === "g:Accounting & Posting" && (
            <>
              <PostingChain payment={payment} trace={trace} />
              <PostingDirection payment={payment} trace={trace} />
            </>
          )}
          {stage.key === "g:Clearing & Settlement" && (
            <>
              <FundsFlow payment={payment} position={settlementPosition} />
              <SettlementOutcomes position={settlementPosition} direction={payment?.direction} />
            </>
          )}
          {grouped ? (
            <>
              {stage.intro && (
                <div className={styles.stageIntro}>
                  <div className={styles.stageIntroLabel}>What this stage does</div>
                  <div className={styles.stageIntroText}>{stage.intro}</div>
                </div>
              )}
              {claimer && (
                <ExceptionsPanel
                  exceptions={claimer.exceptions}
                  payment={payment}
                  onResolve={onResolveException}
                  onLedgerAction={onLedgerAction}
                  onResolveUta={onResolveUta}
                  onAcknowledgeAgent={onAcknowledgeAgent}
                  reversalEvent={claimer.reversalEvent}
                  reversalLegs={claimer.reversalLegs}
                  refreshKey={refreshKey}
                />
              )}
              <Tabs
                aria-label={`${stage.label} panels`}
                selected={panelIdx}
                setSelected={setPanelIdx}
              >
                {stage.children.map((c) => (
                  <Tab key={c.key} name={c.label}>
                    <div className={styles.tabBody}>
                      <div className={styles.groupPanelHead}>
                        {c.status && <StatusPill status={c.status} />}
                        {c.meta && <span className={styles.groupPanelMeta}>{c.meta}</span>}
                      </div>
                      <StageDetailBody
                        stage={{ ...c, exceptions: [] }}
                        payment={payment}
                        {...actions}
                      />
                    </div>
                  </Tab>
                ))}
              </Tabs>
            </>
          ) : (
            <StageDetailBody stage={stage} payment={payment} {...actions} refreshKey={refreshKey} />
          )}
          </div>
          <div className={styles.paneRail}>
            <MongoRail
              writes={writes}
              why={copy?.why}
              sources={sources}
              collapsed={lens === "business"}
            />
          </div>
        </div>
      )}
    </section>
  );
}

const filled = (rows) => rows.filter(([, v]) => v != null && v !== "" && v !== "—");

/** A card of key/values with empty fields left out; nothing at all when no field has a value. */
function SummaryCard({ label, rows, tag }) {
  const kept = filled(rows);
  if (!kept.length) return null;
  return (
    <Card label={label} tag={tag}>
      <KeyValues rows={kept} />
    </Card>
  );
}

/** The stage's state chips, then the recorded transitions with their reason, actor and time. */
function TransitionsCard({ events, stageKey, payment }) {
  return (
    <Card label="State transition">
      <StageStates stageKey={stageKey} payment={payment} />
      {events?.length > 0 && <StateEvents events={events} />}
    </Card>
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
function EnrichmentDiff({ enrichment, inbound }) {
  const resolved = enrichment?.resolved || [];

  if (!enrichment) {
    return <Body className={styles.muted}>No enrichment record on this payment.</Body>;
  }
  if (!resolved.length) {
    return (
      <Body className={styles.muted}>
        {inbound
          ? "No routing or fee enrichment needed for inbound payments: already handled by the sending bank."
          : "Nothing required enrichment — every field was already resolved at initiation."}
      </Body>
    );
  }

  // When nothing was captured for any field, an empty "As captured" column says nothing.
  const hasCaptured = resolved.some((r) => fmtEnriched(r.from) !== "—");
  const cols = hasCaptured ? styles.enrichWithBefore : styles.enrichNoBefore;
  return (
    <div className={`${styles.enrichTable} ${cols}`} role="table">
      <div className={styles.enrichHead} role="row">
        <span>Field</span>
        {hasCaptured && <span>As captured</span>}
        <span>After enrichment</span>
        <span>Source</span>
      </div>
      {resolved.map((r) => (
        <div className={styles.enrichRow} role="row" key={r.field}>
          <span className={styles.enrichField}>{r.field}</span>
          {hasCaptured && <span>{fmtEnriched(r.from) === "—" ? "" : fmtEnriched(r.from)}</span>}
          <span className={styles.enrichValue}>{fmtEnriched(r.to)}</span>
          <span className={styles.enrichSource}>
            <StatusPill family="gray" label={r.source} />
          </span>
        </div>
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
 * After-enrichment pair has room. 
 */
function EnrichmentBody({ stage, payment, checkList }) {
  const events = stage.data?.events || [];
  return (
    <div className={styles.stageStack}>
      {payment?.direction === "INBOUND" ? (
        <InboundValidationChecks payment={payment} checks={checkList} />
      ) : (
        <Card label="Checks" tag="sync and async marked">
          <Checks checks={checkList} />
        </Card>
      )}
      <SummaryCard label="What was determined" rows={summaryRows(stage, payment)} />
      <Card
        label="Progressive enrichment"
        note={
          payment?.direction === "INBOUND"
            ? "Stage 3 is a hard gate: a payment cannot reach the Stage 4 acceptance decision until screening, classification and any required FX conversion are recorded."
            : "Stage 3 is a hard gate: the rail message is only built from a payment that passed here."
        }
      >
        <EnrichmentDiff enrichment={stage.data?.enrichment} inbound={payment?.direction === "INBOUND"} />
      </Card>
      <TransitionsCard events={events} stageKey="validation" payment={payment} />
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
    <div className={`${styles.legs} ${styles.legsLedger}`}>
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
 * run) and render "N/A — book transfer" rather than a tick. A PENDING leg renders on its
 * structured `reason` (never its detail text): AWAITING_STATEMENT — the journal is posted and
 * the correspondent's camt.053 line has not arrived — renders "awaiting the correspondent
 * statement"; anything else (the settlement journal has not posted yet) renders "awaiting
 * the GL batch". The whole thing falls back to a
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
      // leg2's ✗ compares the ACTUAL settled amount (statement-sourced via A2's matcher,
      // null until the correspondent's line matches) against the GL posting — display that
      // actual, not the gross, or a short-settled wire shows the crossed row carrying the
      // same figure as the ticked rows.
      amount: position?.actualAmount ?? position?.grossAmount,
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

  const gap = settlementDelta(position);
  return (
    <div className={styles.reconTieOut}>
      {overall === "DISCREPANT" && gap?.delta > 0 && (
        <div className={styles.failedBanner}>
          Settlement account is short by {fmtAmount(gap.delta, currency)}: expected{" "}
          {fmtAmount(gap.expected, currency)}, the correspondent booked{" "}
          {fmtAmount(gap.actual, currency)}.
        </div>
      )}
      <div className={styles.reconRows}>
        {rows.map((r) => {
          const result = r.leg?.result;
          const isNA = result === "NOT_APPLICABLE";
          const isPending = result === "PENDING";
          // Keyed on the leg's reason, never its detail text (plan A3).
          const pendingText =
            r.leg?.reason === "AWAITING_STATEMENT"
              ? "awaiting the correspondent statement"
              : "awaiting the GL batch";
          return (
            <div className={styles.reconRow} key={r.label}>
              <span className={styles.reconLabel}>{r.label}</span>
              <span className={styles.reconAmount}>
                {isNA || isPending
                  ? (isNA ? r.naText : pendingText)
                  : (r.amount != null ? fmtAmount(r.amount, currency) : "—")}
              </span>
              <StatusPill family={verdictVariant(result)}>{verdictLabel(result)}</StatusPill>
            </div>
          );
        })}
      </div>
      <div className={styles.reconVerdict}>
        <StatusPill family={overall === "RECONCILED" ? "green" : overall === "DISCREPANT" ? "red" : "blue"}>
          {overall === "RECONCILED"
            ? "=> RECONCILED"
            : overall === "DISCREPANT"
              ? "DISCREPANT — discrepancy flagged"
              : (check.legs || []).some((l) => l.reason === "AWAITING_STATEMENT")
                ? "=> pending the correspondent statement"
                : "=> pending the GL batch"}
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
            <span className={styles.eventState}>{e.state}</span>
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
  // CdtTrfTxInf}`, and `CdtTrfTxInf` is 1..n. An inbound payment's stage-5 message is a
  // pacs.002 instead — `Document/FIToFIPmtStsRpt/{GrpHdr, OrgnlGrpInfAndSts,
  // TxInfAndSts}` (pacs002.py MESSAGE_ROOT) — a different message root, same envelope
  // shape. Paths are shown relative to whichever message root is present — repeating the
  // wrapper on every row would be noise.
  const isoBody =
    iso?.Document?.FIToFICstmrCdtTrf ?? iso?.Document?.FIToFIPmtStsRpt ?? iso;
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
            Mapping to ISO 20022 &rarr; {data?.execution?.messageFormat ?? data?.messageFormat}
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
  DOMESTIC: "Domestic",
  CROSS_BORDER: "Cross-border",
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
    case "beneficiaryResolution": {
      // Inbound stage 2 (FR-2.IN1-4) — resolves the CLAIMED creditor from stage 1 against
      // Leafy Bank's own account records. No customer session exists here, so there is no
      // authentication{}/entitlement{} pair to show, unlike outbound's stage 2.
      const r = d;
      return [
        ["Match outcome", r?.matchOutcome],
        ["Matched account", r?.matchedAccountId],
        ["Match method", r?.matchMethod],
        ["Checked at", fmtWhen(r?.checkedAt)],
      ];
    }
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
      // FR-3.12 — the regulatory reports stage 3 attached (cross-border declaration and/or
      // threshold report). An empty array is "assessed, none required" (the spec's "Empty
      // array if none"); absent is "not assessed" and renders "—".
      const reports = payment?.correspondent?.regulatoryReports;
      const reportsLabel = !reports
        ? null
        : reports.length
          ? reports.map((r) => r.reportType).join(", ")
          : "None required";
      // FR-3.20 — who submitted the instruction, distinct from the account holder. Read off
      // the wireDetails envelope (caller-supplied or derived from the authenticated caller);
      // shown here rather than only in the diff so a pass-through value is visible too.
      const initiatingParty = payment?.wireDetails?.initiatingParty;
      // Inbound-only (FR-3.IN1): screens the ORIGINATOR rather than outbound's counterparty.
      // Written to the same `correspondent.sanctionsCheck` object outbound's stage-4 uses,
      // so an inbound payment with no fraud{} still shows the screening it actually ran.
      const originatorScreen = d?.originatorSanctionsCheck;
      return [
        ["Corridor", corridorLabel(payment)],
        ["Originator sanctions screening", originatorScreen
          ? `${originatorScreen.status}${originatorScreen.provider ? ` · ${originatorScreen.provider}` : ""}`
          : null],
        ["Regulatory reports", reportsLabel],
        ["Initiating party", initiatingParty
          ? (initiatingParty.name || initiatingParty.identification)
          : null],
      ];
    }
    case "acceptanceDecision":
      // Inbound stage 4 (FR-4.IN1): the roll-up itself is drawn by AcceptanceRollup.
      return [["Decided", fmtWhen(payment?.acceptanceDecision?.decidedAt)]];
    case "authorization": {
      // The score, rules, sanctions and network are drawn by FraudMeter and RouteMap; this is
      // what is left: the identifiers the checks refer to and the originator confirmation.
      const f = d?.fraud;
      return [
        ["Alert ID", f?.alertId],
        ["Routing snapshot", d?.refs?.routingSnapshotId],
        ["Payment order", d?.refs?.paymentOrderId],
        // FR-4.4 — the originator confirmation (PaymentConfirmation, SD 47766): a persisted
        // artifact written at APPROVED, distinct from stage 5's settlement notification.
        ["Confirmation", payment?.confirmation?.confirmationId ?? null],
        ["Confirmed", fmtWhen(payment?.confirmation?.confirmedAt)],
        ["Authorised", fmtWhen(payment?.clearing?.authorisedAt)],
        ["Assessed", fmtWhen(f?.checkedAt)],
      ];
    }
    case "railExecution": {
      if (payment?.direction === "INBOUND") {
        // Inbound has no execution doc, so most outbound rows are empty. What exists is the
        // pacs.002 sent back to the sending bank and the timestamps on the payment.
        const m = d?.statusResponse;
        return [
          ["Status response", m?.paymentMessageId ?? payment?.refs?.statusResponseMessageId],
          ["Status", m?.statusCode ?? d?.clearing?.statusCode],
          ["Reason code", m?.reason],
          ["Answers message", m?.originalMessageRef],
          ["Sent to sending bank", fmtWhen(d?.clearing?.submittedAt)],
        ];
      }
      // Her L548-553 three lines live in the BUSINESS VIEW tab; this block gives what an
      // operator needs *about the execution* — which attempt, which network, what the rail
      // said back, and the two artifact ids.
      // The rest (message, network, status, attempt, ref, simulated) is in the key facts.
      const e = d?.execution;
      return [
        ["Rail status", d?.railStatus?.code ? `${d.railStatus.code} — ${d.railStatus.reason || ""}` : null],
        ["Network code", d?.clearing?.networkCode],
        ["Settlement date", d?.clearing?.settlementDate],
        ["Payment execution", e?.paymentExecutionId],
        ["Payment message", e?.paymentMessageId],
        ["Submitted", fmtWhen(d?.clearing?.submittedAt)],
        ["Acknowledged", fmtWhen(e?.acknowledgedAt)],
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
        // Stage 6 back-pointer (Doina Sep 17): the journal entry that posted this
        // transaction, stamped by the ledger after the GL batch runs. null until posted.
        ["Journal entry", d?.journalEntryId],
        ["Posted", d?.postedAt ? fmtWhen(d.postedAt) : null],
        ["Created", fmtWhen(d?.createdAt)],
      ];
    case "ledgerEvent":
      return [
        ["Event ID", d?.eventId],
        ["Event type", d?.eventType],
        ["Posting mode", d?.postingMode?.type],
        ["Posting status", d?.postingStatus],
        ["Group", d?.groupId],
        ["Period", d?.periodName],
        ["Value date", d?.valueDate],
        ["Occurred", fmtWhen(d?.occurredAt)],
        ["Posted", d?.postingResult?.postedAt ? fmtWhen(d.postingResult.postedAt) : null],
        ["Journal entry", d?.postingResult?.journalEntryId],
      ];
    case "subLedger": {
      const first = d?.[0];
      return [
        // The accounting-leg identity carried from the ledger event (DR-7.3 / Doina Sep 18) —
        // SETTLEMENT (external settlement posting) vs PAYMENT_PRINCIPAL (initial posting).
        ["Leg type", first?.eventType],
        ["Posting date", fmtWhen(first?.postingDate)],
        ["Period", first?.periodCode],
        ["Journal entry", first?.journalEntryId],
        ["Sub-ledger IDs", (d ?? []).map((e) => e.subLedgerId).filter(Boolean).join(", ") || null],
      ];
    }
    case "journal": {
      // Distinct accounting-leg identities across the merged lines (DR-7.3 / Doina Sep 18) —
      // SETTLEMENT vs PAYMENT_PRINCIPAL — so the merged period journal's lines read as the
      // settlement-leg vs posting-leg aggregation.
      const sourceLegs = [...new Set(
        (d?.entries ?? []).flatMap((e) => e?.sourceEventTypes ?? [])
      )];
      return [
        ["Journal ID", d?.journalId],
        ["Journal type", d?.journalType],
        ["Source legs", sourceLegs.length ? sourceLegs.join(", ") : "—"],
        ["Period", d?.periodCode],
        ["Txn count", d?.sourceReference?.txnCount],
        ["Created by", d?.createdBy],
        ["Created", fmtWhen(d?.createdAt)],
      ];
    }
    case "reconciliation": {
      // Stage 8 — the three-way tie-out summary. The five-row dashboard itself renders in
      // ReconciliationTieOut (below); these rows are the supporting facts: the reconciliation
      // item, the settlement position, the journal that proved leg 3, and when it ran.
      const check = d?.check;
      const pos = d?.position;
      return [
        ["Reconciliation item", check?.legs ? payment?.refs?.reconciliationItemId : null],
        ["Settlement position", pos?.settlementPositionId || payment?.refs?.settlementPositionId],
        ["Journal (leg 3)", check?.journalEntryId],
      ];
    }
    case "legs": {
      // Stage 7 — Clearing & Settlement. `data` is { position, clearing }. Surface the
      // four-way outcome (FR-7.3) and, for UNMATCHED, the discrepancy amount that routes
      // toward the Stage 9 exception queue — not just the settlement status.
      const pos = d?.position;
      const clr = d?.clearing;
      const outcome = pos?.outcome;
      const rows = [
        ["Settlement model", pos?.modelLabel],
        // FR-7.1 (Doina Sep 18) — the event is named to read as the external-settlement posting
        // (eventType: SETTLEMENT, distinct from the stage-6 PAYMENT_PRINCIPAL event).
        ["Settlement event", d?.event?.eventType],
        ["Value date", clr?.settlementDate],
        ["Batch", pos?.batchRef],
      ];
      if (outcome === "UNMATCHED") {
        const ccy = pos?.currency || payment?.currency;
        // Doina's mockup (Sep 17 L1307-1313): "Payment $25,000 / Settlement $24,975 /
        // $25 discrepancy" — show all three so the operator sees WHAT differs, not just a
        // bare delta. Expected = the clearing amount sent; Received = the rail's claimed
        // partial settlement; Discrepancy = the unmatched portion (the correspondent fee).
        rows.push(
          ["Discrepancy", clr?.discrepancyAmount != null ? fmtAmount(clr.discrepancyAmount, ccy) : null],
          ["Discrepancy reason", clr?.discrepancyReason],
        );
      }
      if (clr?.rejectionCode) rows.push(["Rejection code", clr.rejectionCode]);
      if (clr?.returnCode) rows.push(["Return code", clr.returnCode]);
      return rows;
    }
    default:
      return [["Current state", payment?.lifecycle?.currentState]];
  }
}

/**
 * The rail-specific initiation envelope — Doina's "type-specific initiation envelope". One is
 * populated per rail; the other two stay all-null. Shows the fields stage 1 genuinely knows,
 * and says what a later stage resolves (so nulls read as "not yet", not "missing").
 */
function InitEnvelope({ payment }) {
  const rail = payment?.rail;
  const inbound = payment?.direction === "INBOUND";
  let rows = null;
  let note = null;
  if (rail === "WIRE") {
    const w = payment?.wireDetails || {};
    rows = [
      // Inbound arrives as a pacs.008 and is stored in the canonical format; the pain.001
      // identifier the backend stamps there describes an outbound customer instruction.
      ["Message definition", inbound ? "canonical payment format" : w.messageDefinitionIdentifier],
      ["Payment method", w.paymentMethod],
      ["Wire type", w.wireType],
      ["Payment info id", w.paymentInformationId],
      ["Service level", w.paymentTypeInformation?.serviceLevel?.code],
      ["Initiating party", w.initiatingParty?.name],
      // There is no on-behalf-of scenario, so these are null (doc 17 §6). Render an explicit
      // "—" (N/A) rather than dropping the row, so an audience can see the fields exist.
      ["Ultimate debtor", w.ultimateDebtor?.name ?? "—"],
      ["Ultimate creditor", w.ultimateCreditor?.name ?? "—"],
    ];
    note = inbound
      ? "Receive and parse the inbound pacs.008; the payment rail is fixed as WIRE. Routing " +
        "fields (network, local instrument code) stay null, since inbound has no " +
        "orchestration stage to resolve them. The canonical payment is created with only " +
        "the wireDetails envelope populated, and the claimed creditor identity is captured."
      : "Wire type is derived from the two bank countries. Network and local instrument " +
        "code stay empty until stage 4 orchestration.";
  } else if (rail === "INTERNAL") {
    const i = payment?.internalDetails || {};
    rows = [
      ["Transfer type", i.transferType],
      ["Posting reference", i.postingReference],
    ];
    note = "postingReference is filled once the ledger service posts the event.";
  }
  return (
    <div>
      <EnvelopeChips rail={rail} />
      <KeyValues rows={rows ? rows.filter(([, v]) => v != null) : [["Rail", rail || "—"]]} />
      {note && <div className={styles.stageNote}>{note}</div>}
    </div>
  );
}

/**
 * FR-4.1 — the stage-4 execution-strategy decision, rendered from the immutable
 * `routingSnapshots` record. The payment doc carries only `wireDetails.network` + the
 * snapshot id; the strategy label, cost rank, correspondent, cut-off and rationale all live
 * on the snapshot, so without this block the determination is invisible in the UI (Doina:
 * "Cannot test in the UI"). The snapshot is null for a pre-stage-4 payment.
 */
function RoutingDecision({ snapshot }) {
  if (!snapshot) {
    return (
      <Card label="Routing decision">
        <Body className={styles.muted}>
          No routing decision yet — this payment has not reached orchestration (stage 4).
        </Body>
      </Card>
    );
  }
  const corr = snapshot.correspondent || {};
  // Network, value date and cut-off are drawn by RouteMap and CutoffClock, so only the parts
  // those do not show are listed here.
  const rows = [
    ["Cost rank", snapshot.costRank],
    ["Correspondent", corr.required
      ? [corr.bic, corr.bankName, corr.country].filter(Boolean).join(" · ")
      : "none — domestic/intrabank"],
    ["Rationale", snapshot.rationale],
  ];
  return (
    <SummaryCard label="Routing decision" tag="immutable snapshot" rows={rows} />
  );
}

/**
 * Stage 1 as one composed block: who pays whom first, then the instruction and the rail
 * envelope side by side, then the single state transition as a footer. The amount, rail,
 * charge bearer, execution date and channel are in the key facts above, so they are not
 * repeated here, and fields with no value are left out rather than printed as dashes.
 */
function InitiationBody({ payment, initEvents }) {
  const d = payment;
  const inbound = d?.direction === "INBOUND";
  const instruction = [
    ["Type", d?.type],
    ["Priority", d?.priority],
    ["Customer", d?.customerId],
    ["Purpose", d?.remittance?.unstructured],
    ["End-to-end reference", d?.remittance?.reference],
    // An inbound wire carries no client reference; the RECEIVED event below replaces it.
    ...(inbound ? [] : [["Client reference", d?.remittance?.invoiceNo]]),
  ].filter(([, v]) => v != null && v !== "");
  const envelopeLabel = d?.rail === "WIRE" ? (inbound ? "Wire RECEIVED" : "pain.001 envelope") : d?.rail === "INTERNAL" ? "Internal envelope" : "Rail envelope";
  return (
    <div className={styles.initBody}>
      {inbound && <IntakeOrder />}
      <PartyFlow payment={d} />
      <div className={styles.initDetails}>
        <div className={styles.partyCard}>
          <div className={styles.partyLabel}>Instruction</div>
          <KeyValues rows={instruction} />
        </div>
        <div className={styles.partyCard}>
          <div className={styles.partyLabel}>{envelopeLabel}</div>
          <InitEnvelope payment={d} />
        </div>
      </div>
      {initEvents.length > 0 && (
        <Card label="State transition">
          <StatePath events={d?.lifecycle?.events} inbound={inbound} />
          <StateEvents events={initEvents} />
        </Card>
      )}
    </div>
  );
}

function StageDetailBody({ stage, payment, onResolveException, onResolveUta, onLedgerAction, onAcknowledgeAgent, refreshKey = 0 }) {
  if (!stage) return null;

  if (!stage.reached) {
    return (
      <Body className={styles.muted}>
        {stage.label} — not reached yet.
      </Body>
    );
  }

  const { kind, key } = stage;
  const events = stage.data?.events;
  // Stages 3, 4 and 5 keep their checks in an object; stage 2 passes the bare array.
  const checkList = ["enrichment", "authorization", "acceptanceDecision", "railExecution"].includes(kind)
    ? stage.data?.checks
    : stage.data;
  // Stage 1's only lifecycle event is INITIATED (outbound) or RECEIVED (inbound) — the
  // moment the instruction was captured or the message arrived.
  const stageOneState = payment?.direction === "INBOUND" ? "RECEIVED" : "INITIATED";
  const initEvents = (payment?.lifecycle?.events || []).filter(
    (e) => (e.state || "").toUpperCase() === stageOneState
  );
  const hasChecks = !!checkList?.length;
  const dedicated = [
    "initiation", "checks", "beneficiaryResolution", "enrichment", "authorization",
    "acceptanceDecision", "railExecution", "reconciliation", "states",
  ].includes(kind);
  const inbound = payment?.direction === "INBOUND";
  const reconTitle = payment?.rail === "INTERNAL"
    ? "Payment to general ledger"
    : inbound
      ? "Received message and pacs.002 to ledger, rail to settlement account, settlement account to GL"
      : "Three-way match: payment to rail, rail to settlement account, settlement account to GL";

  return (
    <>
      {stage.intro && (
        <div className={styles.stageIntro}>
          <div className={styles.stageIntroLabel}>What this stage does</div>
          <div className={styles.stageIntroText}>{stage.intro}</div>
        </div>
      )}
      <div className={styles.detailColumns}>
        {kind === "initiation" && (
          <div className={styles.detailBlockWide}>
            <InitiationBody payment={stage.data} initEvents={initEvents} />
          </div>
        )}

        {kind === "checks" && (
          <div className={styles.stageStack}>
            <GateCards payment={payment} checks={checkList} />
            <LimitGauge payment={payment} />
            <TransitionsCard stageKey={key} payment={payment} />
          </div>
        )}

        {kind === "beneficiaryResolution" && (
          <div className={styles.stageStack}>
            <InboundResolutionChecks
              payment={payment}
              checks={(payment?.checks || []).filter((c) => String(c?.stage || "").startsWith("2 "))}
            />
            <ResolutionOutcome payment={payment} />
            <TransitionsCard stageKey={key} payment={payment} />
          </div>
        )}

        {kind === "states" && (
          <Card label="State transitions">
            <StateEvents events={stage.data} />
          </Card>
        )}

        {kind === "enrichment" && (
          <EnrichmentBody stage={stage} payment={payment} checkList={checkList} />
        )}

        {kind === "authorization" && (
          <div className={styles.stageStack}>
            <RouteMap snapshot={stage.data?.routingSnapshot} payment={payment} />
            <div className={styles.cardGrid}>
              <RoutingDecision snapshot={stage.data?.routingSnapshot} />
              <FraudMeter fraud={stage.data?.fraud} sanctions={stage.data?.sanctions} />
            </div>
            <SummaryCard label="References" rows={summaryRows(stage, payment)} />
            {hasChecks && (
              <Card label="Checks"><Checks checks={checkList} /></Card>
            )}
            <TransitionsCard events={events} stageKey={key} payment={payment} />
          </div>
        )}

        {kind === "acceptanceDecision" && (
          <div className={styles.stageStack}>
            <AcceptanceRollup payment={payment} />
            {hasChecks && (
              <Card label="Checks"><Checks checks={checkList} /></Card>
            )}
            <SummaryCard label="Decision record" rows={summaryRows(stage, payment)} />
            <TransitionsCard events={events} stageKey={key} payment={payment} />
          </div>
        )}

        {kind === "railExecution" && (
          <div className={styles.stageStack}>
            <RailFlow payment={payment} data={stage.data} />
            <SummaryCard label="Rail response" rows={summaryRows(stage, payment)} />
            <Card
              label={inbound ? "Business view and pacs.002" : "Canonical payment to ISO 20022"}
              note={inbound
                ? "The payment has not settled on Leafy Bank's books yet, and will only do so if it is ACCEPTED. A simulated pacs.002 is created in the canonicalJsonStorage collection, confirming or rejecting the payment."
                : "The ISO message is built only at the rail boundary, never earlier in the lifecycle."}
            >
              <RailViews data={stage.data} />
            </Card>
            {hasChecks && (
              <Card label="Checks"><Checks checks={checkList} /></Card>
            )}
            <TransitionsCard events={events} stageKey={key} payment={payment} />
          </div>
        )}

        {kind === "reconciliation" && (
          <div className={styles.stageStack}>
            <Card label={reconTitle}>
              <ReconciliationTieOut data={stage.data} payment={payment} />
            </Card>
            <SummaryCard label="References" rows={summaryRows(stage, payment)} />
            <TransitionsCard events={events} stageKey={key} payment={payment} />
          </div>
        )}

        {!dedicated && (
          <SummaryCard label="Summary" rows={summaryRows(stage, payment)} />
        )}

        {(stage.exceptions || []).length > 0 && (
          <ExceptionsPanel
            exceptions={stage.exceptions}
            payment={payment}
            onResolve={onResolveException}
            onLedgerAction={onLedgerAction}
            onResolveUta={onResolveUta}
            onAcknowledgeAgent={onAcknowledgeAgent}
            reversalEvent={stage.reversalEvent}
            reversalLegs={stage.reversalLegs}
            refreshKey={refreshKey}
          />
        )}

        {!!stage.legs && (
          <div style={{ gridColumn: "1 / -1", minWidth: 0 }}>
            <Card label="Double-entry">
              <Legs legs={stage.legs} />
            </Card>
          </div>
        )}
      </div>
    </>
  );
}

/**
 * Stage 9 — the Exceptions panel (doc 24 §3 step 8). Renders the payment's exception
 * occurrences (joined by `get_payment`), the resolution log for a closed one, and — for an
 * OPEN exception — the per-category resolve CTAs modeled on the stage-4 review callout.
 *
 * The action set per category mirrors the backend `_LEGAL` map (B4's table). The payment's
 * terminal state is never changed by a resolve (B4) — the copy says so. RETRY_SETTLEMENT is
 * the only action that needs a secondary input (the simulated settlement outcome to re-drive).
 */
const EXCEPTION_ACTIONS = {
  SETTLEMENT_DELAYED: ["RETRY_SETTLEMENT"],
  SETTLEMENT_UNMATCHED: ["RETURN_FUNDS", "ACCEPT_DISCREPANCY"],
  SETTLEMENT_RETURNED: ["RETURN_FUNDS"],
  // Plan A4. DISCREPANCY's closing action depends on chargeBearer — see `actionsFor`.
  RECONCILIATION_DISCREPANCY: ["RECHECK", "ACCEPT_DISCREPANCY", "POST_ADJUSTMENT", "ESCALATE_TO_CORRESPONDENT"],
  RECONCILIATION_MISSING: ["RECHECK", "LINK_STATEMENT_ENTRY", "ESCALATE_TO_CORRESPONDENT"],
  ORPHANED_SETTLEMENT: ["LINK_STATEMENT_ENTRY", "ESCALATE_TO_CORRESPONDENT", "DISMISS"],
  DUPLICATE_SIGNAL: ["DISMISS"],
  // Inbound only (FR-9.IN2). Structurally different from every action above: REPAIR
  // resumes the payment at stage 3 after an operator confirms the beneficiary, RETURN
  // sends a pacs.004 back to the sender. They post to a different route (`/uta`) because
  // each needs a field none of the outbound actions has.
  UTA: ["REPAIR", "RETURN"],
};
const ACTION_LABELS = {
  RETRY_SETTLEMENT: "Retry settlement",
  RETURN_FUNDS: "Return funds",
  ACCEPT_DISCREPANCY: "Accept discrepancy",
  DISMISS: "Dismiss",
  REPAIR: "Repair — confirm match",
  RETURN: "Return via pacs.004",
  RECHECK: "Re-check statement",
  LINK_STATEMENT_ENTRY: "Link statement line",
  POST_ADJUSTMENT: "Book correspondent charge",
  ESCALATE_TO_CORRESPONDENT: "Escalate to correspondent",
};

// Plan A4 — these two run on the ledger (`/pipeline/exceptions/{id}/…`), not `/resolve`.
const LEDGER_ACTIONS = { RECHECK: "recheck", LINK_STATEMENT_ENTRY: "link" };

// Mirrors the backend's chargeBearer gate (parent plan Decision 2): DEBT means the bank
// absorbs the correspondent's charge and books it; any other bearer accepts with no entry.
// The backend refuses the wrong one either way — this only hides a button that would 422.
function actionsFor(exception, payment) {
  const actions = EXCEPTION_ACTIONS[exception.category] || [];
  if (exception.category !== "RECONCILIATION_DISCREPANCY") return actions;
  const isDebt = payment?.chargeBearer === "DEBT";
  return actions.filter((a) =>
    a === "POST_ADJUSTMENT" ? isDebt : a === "ACCEPT_DISCREPANCY" ? !isDebt : true
  );
}

function linkCandidateLabel(exc) {
  const d = exc.detail || {};
  if (exc.category === "ORPHANED_SETTLEMENT") {
    return `${d.reference || "line"} · ${d.currency || ""} ${d.actualAmount ?? "?"}`;
  }
  return `${exc.paymentId} · expected ${d.expectedAmount ?? "?"}`;
}

// A MISSING payment's own unmatched statement lines, shaped like the orphan twins the picker
// lists. Without them a wrongly dismissed orphan leaves the payment with nothing to link.
function withUnclaimedStatementLines(twins, open, payment) {
  if (open?.category !== "RECONCILIATION_MISSING") return twins;
  const seen = new Set(twins.map((t) => `${t.subjectRef?.paymentMessageId}#${t.subjectRef?.lineNo}`));
  const lines = (payment?.statements || []).flatMap((stmt) =>
    (stmt.entries || [])
      .filter((e) => e.simulatedPaymentId === payment.paymentId && e.recon?.status === "UNMATCHED")
      .map((e) => ({
        exceptionId: `${stmt.paymentMessageId}#${e.lineNo}`,
        category: "ORPHANED_SETTLEMENT",
        subjectRef: { paymentMessageId: stmt.paymentMessageId, lineNo: e.lineNo },
        detail: { reference: e.reference, actualAmount: e.amount, currency: e.currency },
      }))
  );
  return [...twins, ...lines.filter((l) => !seen.has(l.exceptionId))];
}

// The UTA actions post to `/workflow/exceptions/{id}/uta`, not `/resolve`. Named here so
// the panel routes by data rather than by a hardcoded category check at the call site.
const UTA_ACTIONS = new Set(["REPAIR", "RETURN"]);

// ISO 20022 ExternalReturnReason1Code — the subset the backend's pacs.004 builder accepts.
// Mirrors `pacs004.RETURN_REASON_CODES`; an operator picks the reason the sender will see.
const RETURN_REASONS = [
  ["AC01", "AC01 — Incorrect account number"],
  ["AC04", "AC04 — Closed account number"],
  ["RR04", "RR04 — Regulatory reason"],
  ["MS03", "MS03 — No reason specified"],
];
const SEVERITY_LABEL = {
  ACTION_REQUIRED: "Action required",
  INFORMATIONAL: "Informational",
};
// Plain-language explanation of what each exception category means — so the operator sees
// what actually happened, not just a category pill. Rendered as a line under the detail.
const EXCEPTION_EXPLANATION = {
  SETTLEMENT_DELAYED:
    "The rail accepted the payment but settlement is scheduled for a future value date — " +
    "the money has not moved yet. Retry settlement once the value date arrives, or hold.",
  SETTLEMENT_UNMATCHED:
    "The rail settled for less than the expected amount — a partial short-pay. The bank " +
    "treats this as a failed settlement: return the full funds, or accept the discrepancy.",
  SETTLEMENT_RETURNED:
    "The rail returned the payment and no settlement occurred. Return the funds to the debtor.",
  RECONCILIATION_DISCREPANCY:
    "The three-way reconciliation — payment to rail, rail to settlement, settlement to GL — " +
    "found a mismatch, e.g. the rail settled short of the GL posting. Who bears the charges " +
    "decides the fix: if Leafy Bank does (DEBT), book the charge (Dr 5214 Correspondent " +
    "Charges / Cr nostro) and reconciliation closes once it posts; otherwise the beneficiary " +
    "bore it, so accept the discrepancy with no entry.",
  RECONCILIATION_MISSING:
    "The payment settled, but the correspondent bank's statement has not shown it within " +
    "the expected window. It may be a timing lag, a reference the correspondent re-keyed, " +
    "or a booking that never happened. It clears on its own if the statement line arrives; " +
    "re-check now, link it to an unclaimed statement line, or escalate to the correspondent.",
  ORPHANED_SETTLEMENT:
    "The correspondent bank's statement shows a booking that no Leafy Bank payment claims. " +
    "It may belong to a payment whose reference the correspondent changed, or be an entry " +
    "the bank did not originate. Link it to the payment it belongs to, escalate, or dismiss it.",
  DUPLICATE_SIGNAL:
    "This payment resembles an earlier one — a possible duplicate submission. Dismiss if " +
    "the duplication is intentional.",
  UTA:
    "An incoming payment arrived that cannot be credited as instructed — the beneficiary " +
    "could not be confirmed, or acceptance was refused. The funds are held in the wire " +
    "clearing account: confirm the correct beneficiary to apply them, or return them to " +
    "the sending bank.",
};

function exceptionDetailText(exc) {
  const d = exc?.detail || {};
  if (exc?.category === "DUPLICATE_SIGNAL" && d.duplicateOf) return `Resembles ${d.duplicateOf}`;
  if (exc?.category === "UTA") {
    // Her L1341-1343 demo panel: the claimed beneficiary, and the closest thing we found.
    // Both come off `detail` so the operator never has to open the raw message.
    const claimed = d.claimedName ? `"${d.claimedName}"` : "unnamed beneficiary";
    const closest = d.closestAccountId ? ` · closest match ${d.closestAccountId}` : "";
    return `${claimed}${d.claimedAccountNo ? ` · a/c ${d.claimedAccountNo}` : ""} — ${
      d.matchOutcome || "no match"
    }${closest}`;
  }
  const disc = d.discrepancyAmount;
  if (disc != null) {
    const n = Number(disc);
    const s = Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`;
    return `${s} discrepancy${d.discrepancyReason ? ` — ${d.discrepancyReason}` : ""}`;
  }
  if (d.returnCode) return `Return code ${d.returnCode}`;
  return "—";
}

// Mirrors payment_agent MAX_ERROR_ATTEMPTS.
const MAX_AGENT_ATTEMPTS = 5;

function ExceptionsPanel({ exceptions, payment, onResolve, onResolveUta, onLedgerAction, reversalEvent, reversalLegs, refreshKey = 0, onAcknowledgeAgent }) {
  const baseOpen = exceptions.find((e) => e?.status === "OPEN");
  // The Reconciliation Agent writes `exceptions.agent{}` asynchronously, after the exception
  // opens. Fetch it on a dedicated refresh so the operator sees the agent's findings appear
  // without waiting for the 10s batch tick to re-pull the whole workflow.
  const { agent: agentBlock, loading: agentLoading } = useReconciliationAgent(
    baseOpen?.exceptionId,
    refreshKey
  );
  const open = baseOpen && agentBlock ? { ...baseOpen, agent: agentBlock } : baseOpen;
  const [note, setNote] = useState("");
  const [outcome, setOutcome] = useState("MATCHED");
  const [busy, setBusy] = useState(false);
  const [approving, setApproving] = useState(false);
  // Errors from this panel's own actions render here, next to the button that failed —
  // not in the deep-dive's page-level banner, whose copy is about manual review.
  const [panelError, setPanelError] = useState(null);
  // The approve route resumes the agent graph but persists nothing on the exception, so
  // track acknowledgement locally to stop the button being offered twice.
  const [acknowledged, setAcknowledged] = useState(() => new Set());
  // UTA only. The account an operator confirms as the true beneficiary (Repair), and the
  // ISO reason the sending bank will see (Return). Pre-filled from the exception's own
  // `closestAccountId` so the common case — confirming the match the system already found —
  // is one click, which is exactly what her demo panel shows.
  const [repairAccount, setRepairAccount] = useState("");
  const [returnReason, setReturnReason] = useState("AC01");
  const [linkTarget, setLinkTarget] = useState("");
  const twinCandidates = useLinkCandidates(baseOpen?.category, refreshKey);
  const linkCandidates = withUnclaimedStatementLines(twinCandidates, baseOpen, payment);

  if (!exceptions.length) {
    return <Body className={styles.muted}>No exceptions recorded for this payment.</Body>;
  }

  // The rows the panel renders. The open exception carries the freshly-fetched `agent{}`
  // block: `get_payment`'s join only re-runs on the whole-workflow refetch, so without this
  // substitution the dedicated `useReconciliationAgent` fetch above would have no effect on
  // what is displayed and the AI investigation would appear minutes late.
  const rows = open
    ? exceptions.map((e) => (e.exceptionId === open.exceptionId ? open : e))
    : exceptions;

  const actions = open ? actionsFor(open, payment) : [];
  const canLink = actions.includes("LINK_STATEMENT_ENTRY");
  const needsOutcome = actions.includes("RETRY_SETTLEMENT");
  const isUta = open?.category === "UTA";
  // The account the system found but could not confirm. Offered as the default so the
  // operator confirms a specific suggestion rather than typing an id from memory.
  const suggestedAccount = open?.detail?.closestAccountId || "";

  async function doResolve(action) {
    if (!open) return;
    setBusy(true);
    setPanelError(null);
    let err = null;
    try {
    if (LEDGER_ACTIONS[action]) {
      if (onLedgerAction) {
        const twin = linkCandidates.find((c) => c.exceptionId === linkTarget);
        err = await onLedgerAction(open.exceptionId, LEDGER_ACTIONS[action], {
          note: note || undefined,
          // From an orphan the target is a payment; from a MISSING it is the orphan's line.
          ...(action === "LINK_STATEMENT_ENTRY" && twin
            ? open.category === "ORPHANED_SETTLEMENT"
              ? { paymentId: twin.paymentId }
              : {
                  paymentMessageId: twin.subjectRef?.paymentMessageId,
                  lineNo: twin.subjectRef?.lineNo,
                }
            : {}),
        });
      }
    } else if (UTA_ACTIONS.has(action)) {
      // Different route, different fields — see `UTA_ACTIONS`.
      if (onResolveUta) {
        err = await onResolveUta(open.exceptionId, action, {
          matchedAccountId:
            action === "REPAIR" ? repairAccount || suggestedAccount : undefined,
          returnReasonCode: action === "RETURN" ? returnReason : undefined,
          note: note || undefined,
        });
      }
    } else if (onResolve) {
      err = await onResolve(open.exceptionId, action, {
        note: note || undefined,
        newSettlementOutcome: action === "RETRY_SETTLEMENT" ? outcome : undefined,
      });
    }
    } finally {
      setBusy(false);
    }
    // Keep the operator's note when the action failed so they can retry without retyping.
    if (err) setPanelError(`${ACTION_LABELS[action] || action} failed — ${err}`);
    else {
      setNote("");
      setLinkTarget("");
    }
  }

  return (
    <div className={styles.exceptionsPanel}>
      {/* The "why we stopped here" banner — the stage-level explanation of what happened.
          The exception category names a failure mode; this line tells the operator what that
          failure mode *means* in plain language, right at the stage where the payment halted. */}
      {(open || exceptions[0]) && EXCEPTION_EXPLANATION[(open || exceptions[0]).category] && (
        <div className={styles.resolveCallout}>
          <div className={styles.resolveCalloutTitle}>
            <Icon glyph="InfoWithCircle" />
            <span>What happened here</span>
          </div>
          <Body>{EXCEPTION_EXPLANATION[(open || exceptions[0]).category]}</Body>
        </div>
      )}
      {panelError && <Banner variant="danger">{panelError}</Banner>}
      {open && !open.agent && agentLoading && (
        <div className={styles.resolveCallout}>
          <div className={styles.resolveCalloutTitle}>
            <Icon glyph="Refresh" />
            <span>AI investigation in progress…</span>
          </div>
          <Body className={styles.muted}>
            The Reconciliation Agent is gathering the payment&apos;s records. Its findings
            appear here when it finishes.
          </Body>
        </div>
      )}
      {rows.map((e) => (
        <div key={e.exceptionId} className={styles.exceptionRow}>
          <div className={styles.exceptionRowHead}>
            <StatusPill status={e.category} />
            <span className={styles.exceptionSeverity}>
              {SEVERITY_LABEL[e.severity] || e.severity}
            </span>
            <span className={styles.exceptionStatus}>{e.status}</span>
            {e.status === "OPEN" && e.awaitingCounterparty && (
              <StatusPill family="yellow">Awaiting correspondent</StatusPill>
            )}
          </div>
          <div className={styles.exceptionDetail}>{exceptionDetailText(e)}</div>
          {e.agent && (
            <div className={styles.resolveCallout}>
              <div className={styles.resolveCalloutTitle}>
                <Icon glyph="InfoWithCircle" />
                <span>AI investigation</span>
                <StatusPill
                  family={
                    e.agent.confidence === "HIGH"
                      ? "green"
                      : e.agent.confidence === "MEDIUM"
                        ? "yellow"
                        : "gray"
                  }
                >
                  {e.agent.confidence}
                </StatusPill>
              </div>
              <Body>{e.agent.rootCause}</Body>
              {e.agent.error && (
                <Body className={styles.muted}>
                  The investigation failed ({e.agent.error.message}). It retries automatically
                  every minute, up to {MAX_AGENT_ATTEMPTS} attempts.
                </Body>
              )}
              {e.agent.recommendedResolution && (
                <Body className={styles.muted}>
                  Recommend: {e.agent.recommendedResolution}
                </Body>
              )}
              {e.agent.evidence && e.agent.evidence.length > 0 && (
                <ul className={styles.agentEvidence}>
                  {e.agent.evidence.map((ev, i) => (
                    <li key={i}>{ev}</li>
                  ))}
                </ul>
              )}
              {onAcknowledgeAgent && e.status === "OPEN" && e.agent.proposedAction && !acknowledged.has(e.exceptionId) && (
                <div className={styles.resolveActions}>
                  <Button
                    size="xsmall"
                    variant="primary"
                    disabled={approving}
                    onClick={async () => {
                      setApproving(true);
                      setPanelError(null);
                      let err = null;
                      try {
                        err = await onAcknowledgeAgent(e.exceptionId);
                      } finally {
                        setApproving(false);
                      }
                      if (err) setPanelError(`Approve failed — ${err}`);
                      else setAcknowledged((prev) => new Set(prev).add(e.exceptionId));
                    }}
                  >
                    Approve AI proposal: {e.agent.proposedAction.action}
                  </Button>
                </div>
              )}
            </div>
          )}
          {e.resolution && (
            <div className={styles.resolutionLog}>
              <span className={styles.resolutionLabel}>Resolved:</span>
              <span>
                {ACTION_LABELS[e.resolution.action] || e.resolution.action} by {e.resolution.by}
              </span>
              {e.resolution.note && (
                <span className={styles.resolutionNote}> — {e.resolution.note}</span>
              )}
            </div>
          )}
        </div>
      ))}

      {open && (onResolve || onResolveUta || onLedgerAction) && actions.length > 0 && (
        <div className={styles.resolveCallout}>
          <div className={styles.resolveCalloutTitle}>
            <Icon glyph="Diagram3" />
            <span>{isUta ? "Unable to apply — resolve" : "Resolve this exception"}</span>
          </div>
          <Body>
            {isUta
              ? "Confirm the beneficiary to apply the funds, or return them to the sending " +
                "bank with a pacs.004. A repair resumes the payment at validation and " +
                "credits the customer; a return credits no one."
              : "Select an action. The payment's terminal state is not changed — resolution " +
                "is evidence alongside it. A return of funds posts a compensating movement " +
                "that restores the debtor and clears the clearing account."}
          </Body>
          {isUta && (
            <>
              <div className={styles.resolveField}>
                <label className={styles.resolveFieldLabel} htmlFor="uta-account">
                  Beneficiary account to credit
                </label>
                <TextInput
                  id="uta-account"
                  size="small"
                  placeholder={suggestedAccount || "Account id"}
                  value={repairAccount}
                  onChange={(e) => setRepairAccount(e.target.value)}
                />
              </div>
              <div className={styles.resolveField}>
                <label className={styles.resolveFieldLabel} htmlFor="uta-reason">
                  Return reason (pacs.004)
                </label>
                <Select
                  id="uta-reason"
                  size="small"
                  value={returnReason}
                  onChange={setReturnReason}
                  allowDeselect={false}
                >
                  {RETURN_REASONS.map(([code, label]) => (
                    <Option key={code} value={code}>
                      {label}
                    </Option>
                  ))}
                </Select>
              </div>
            </>
          )}
          {canLink && (
            <div className={styles.resolveField}>
              <label className={styles.resolveFieldLabel} htmlFor="exc-link-target">
                {open.category === "ORPHANED_SETTLEMENT"
                  ? "Payment this line belongs to"
                  : "Unclaimed statement line"}
              </label>
              <Select
                id="exc-link-target"
                size="small"
                placeholder={linkCandidates.length ? "Select…" : "No open candidates"}
                value={linkTarget}
                onChange={setLinkTarget}
                disabled={!linkCandidates.length}
              >
                {linkCandidates.map((c) => (
                  <Option key={c.exceptionId} value={c.exceptionId}>
                    {linkCandidateLabel(c)}
                  </Option>
                ))}
              </Select>
            </div>
          )}
          {needsOutcome && (
            <div className={styles.resolveField}>
              <label className={styles.resolveFieldLabel} htmlFor="exc-settlement-outcome">
                Simulated settlement outcome
              </label>
              <Select
                id="exc-settlement-outcome"
                size="small"
                value={outcome}
                onChange={setOutcome}
                allowDeselect={false}
              >
                <Option value="MATCHED">Matched — settle</Option>
                <Option value="UNMATCHED">Unmatched — fail again</Option>
                {/* DELAYED disabled — no external rail to wait on; re-enable with a re-query hook. */}
                {/* <Option value="DELAYED">Delayed — hold again</Option> */}
                <Option value="EXCEPTION">Exception — return</Option>
              </Select>
            </div>
          )}
          <div className={styles.resolveField}>
            <label className={styles.resolveFieldLabel} htmlFor="exc-note">Note</label>
            <TextInput
              id="exc-note"
              size="small"
              placeholder="e.g. Correspondent fee — accepted with cause"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </div>
          <div className={styles.resolveActions}>
            {actions.map((a) => (
              <Button
                key={a}
                size="small"
                // RETURN is destructive in the sense that matters here — no customer is
                // credited and the payment closes — so it does not get the primary styling
                // that would make it the obvious default next to Repair.
                variant={a === "DISMISS" || a === "RETURN" ? "default" : "primary"}
                // Repair needs an account: either the operator typed one or the system
                // suggested one. Without this the button posts an empty id and 422s.
                disabled={
                  busy ||
                  (a === "REPAIR" && !repairAccount && !suggestedAccount) ||
                  (a === "LINK_STATEMENT_ENTRY" && !linkTarget) ||
                  (a === "ESCALATE_TO_CORRESPONDENT" && open.awaitingCounterparty)
                }
                onClick={() => doResolve(a)}
              >
                {ACTION_LABELS[a] || a}
              </Button>
            ))}
          </div>
        </div>
      )}

      {reversalEvent && (
        <div className={styles.exceptionRow}>
          <div className={styles.exceptionRowHead}>
            <StatusPill status="REVERSAL" />
            <span className={styles.exceptionSeverity}>Compensating movement</span>
            <span className={styles.exceptionStatus}>{reversalEvent.postingStatus}</span>
          </div>
          <div className={styles.exceptionDetail}>
            {reversalEvent.postingStatus === "POSTED"
              ? `Funds returned — posted${
                  reversalEvent.postingResult?.journalEntryId
                    ? ` · ${reversalEvent.postingResult.journalEntryId}`
                    : ""
                }`
              : "Funds returned — in the GL pipeline, awaiting the batch"}
          </div>
          {reversalLegs && (
            <div className={styles.detailBlock}>
              <div className={styles.detailBlockTitle}>Reversal posting</div>
              <Legs legs={reversalLegs} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function PaymentDeepDive({ paymentId, refreshKey, onBack, onDataChanged }) {
  // 2026-09-09 (Kiran): the step-up approval happens HERE, at stage 2, not at initiate. A held
  // payment is resumed from this view; `nudge` bumps into the hook's refresh key so the
  // lifecycle re-renders once the resume advances it past INITIATED.
  const [nudge, setNudge] = useState(0);
  const [stepUpOpen, setStepUpOpen] = useState(false);
  const [stepUpError, setStepUpError] = useState(null);
  const [resolveError, setResolveError] = useState(null);
  const { payment, loading, error } = usePaymentWorkflow(
    paymentId,
    (refreshKey || 0) + nudge
  );
  // Batch tick — re-fetch the payment + re-arm the trace when the GL batch actually posts,
  // so a RETURN_FUNDS reversal (async via CDC + batch) surfaces in the accounting panels
  // without a manual refresh. `lastBatchAt` only changes when a batch runs, so this is not
  // a busy poll. Bumping `nudge` feeds both the payment refresh key above and the trace's
  // `resumeKey` below.
  const lastBatchAt = useBatchTick(!!paymentId, 10000);
  useEffect(() => {
    if (lastBatchAt) setNudge((n) => n + 1);
  }, [lastBatchAt]);
  // Ledger half. Self-terminating poll — stops once the journal entry lands. `nudge` as the
  // resume key re-arms it after a resolve/review/resume and after each batch post, so the
  // reversal event (idempotencyKey {paymentId}-REV) is picked up once the batch journals it.
  const { trace } = usePipelineTrace(paymentId, !!paymentId, 2000, (refreshKey || 0) + nudge);
  const [selectedKey, setSelectedKey] = useState("initiation");
  const [lens, setLens] = useLens();

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
    // B6: the list (Operations/Activity) must refetch on Back so a resumed payment's new
    // state shows — `nudge` only refreshes this deep-dive. `onDataChanged` bumps the shared
    // refreshKey the list hooks depend on.
    if (onDataChanged) onDataChanged();
  }

  // FR-4.13 — an operator's manual-review decision on a payment held at PENDING_REVIEW.
  // Approve commits the authorisation and continues the payment to execution; decline
  // terminates it to REJECTED. `nudge` refreshes the lifecycle once the saga advances.
  async function resolveReview(decision) {
    if (!paymentId) return;
    setResolveError(null);
    const { error: err } = await coreApi("TransactionAuthorization/Resolve", {
      method: "POST",
      body: { paymentId, decision },
    });
    if (err) {
      setResolveError(err);
      return;
    }
    setNudge((n) => n + 1);
    if (onDataChanged) onDataChanged();
  }

  // Stage 9 — resolve a queued exception (doc 24 B4). The action + optional note + optional
  // settlement outcome POST to the one /workflow write route. `nudge` refreshes the lifecycle
  // so the resolution log + the compensation evidence (for RETURN_FUNDS) appear.
  async function resolveException(excId, action, { note, newSettlementOutcome } = {}) {
    if (!excId) return;
    const { error: err } = await coreApi(
      `workflow/exceptions/${excId}/resolve`,
      { method: "POST", body: { action, note, newSettlementOutcome } }
    );
    // The panel renders its own errors next to the action — return, don't banner.
    if (err) return err;
    setNudge((n) => n + 1);
    // B6: bump the shared refreshKey so the Operations queue refetches — without this, hitting
    // Back after a resolve shows the stale OPEN row (the list hooks never re-ran). The
    // exception is now RESOLVED/DISMISSED server-side; the queue row must reflect that.
    if (onDataChanged) onDataChanged();
  }

  // The inbound queue's resolve (FR-9.IN2). A separate route from `resolveException`
  // because REPAIR and RETURN carry fields none of the outbound actions has — see
  // `UTA_ACTIONS`. Same refresh discipline (defect 2026-09-28 B6): bump BOTH the detail's
  // nudge and the list's shared key, or hitting Back shows the stale OPEN row.
  async function resolveUta(excId, action, { matchedAccountId, returnReasonCode, note } = {}) {
    if (!excId) return;
    const { error: err } = await coreApi(
      `workflow/exceptions/${excId}/uta`,
      { method: "POST", body: { action, matchedAccountId, returnReasonCode, note } }
    );
    if (err) return err;
    setNudge((n) => n + 1);
    if (onDataChanged) onDataChanged();
  }

  // Plan A4 — RECHECK and LINK_STATEMENT_ENTRY run on the ledger. A recheck that finds
  // nothing new is a 200 with the exception still OPEN; say so rather than look inert.
  async function ledgerAction(excId, route, body = {}) {
    if (!excId) return;
    const { data, error: err } = await pipelineApi(
      `exceptions/${excId}/${route}`, null, { method: "POST", body }
    );
    if (err) return err;
    setNudge((n) => n + 1);
    if (onDataChanged) onDataChanged();
    if (route === "recheck" && data?.exception?.status === "OPEN") {
      return `still ${data.outcome?.toLowerCase() || "open"} — no new statement evidence yet`;
    }
  }

  // HITL approval gate (recon plan Part C): the Reconciliation Agent's graph paused at its
  // `approval` node with a proposed action. APPROVE resumes it; the agent then executes the
  // action through the same transactions/ledger route an operator would, and verifies the
  // result. The full agent card (Approve/Reject, evidence, candidates) is Part D.
  async function acknowledgeAgent(excId) {
    if (!excId) return;
    const { error: err } = await agentApi(
      `reconciliation/${excId}/approve`,
      null,
      { method: "POST", body: { decision: "APPROVE" } }
    );
    if (err) return err;
    // Bump the dedicated agent-investigation refresh so the callout re-renders resolved,
    // and the shared refresh so the queue reflects the acknowledged state.
    setNudge((n) => n + 1);
    if (onDataChanged) onDataChanged();
  }
  // Per-payment guard so the failed-stage auto-expand fires once, not on every trace re-poll.
  const autoExpandedFor = useRef(null);

  // Reset to the first stage when a different payment is opened — keyed on paymentId, not
  // on the trace, so the 2s re-polls don't clobber the current selection every tick.
  useEffect(() => {
    setSelectedKey("initiation");
    autoExpandedFor.current = null;
  }, [paymentId]);

  // Group first so the stepper reads as Doina's 8 stages — stage 6 as one
  // "Accounting & Posting" node with the four panels nested — rather than ~12 nodes.
  const stages = useMemo(
    () => (payment ? groupLifecycleStages(buildLifecycleStages(payment, trace), payment.direction) : null),
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
    // Auto-expand the stage that owns an OPEN exception — that is where the resolve CTAs
    // now render (the remedy lives at the failure site). Fall back to the failing stage's
    // red banner when a payment failed without producing an exception doc. Runs once per
    // payment.
    const failedIdx = states.indexOf("failed");
    const openExcStage = stages.find((s) =>
      (s.exceptions || []).some((e) => e?.status === "OPEN")
    );
    if (openExcStage) {
      setSelectedKey(openExcStage.key);
      autoExpandedFor.current = paymentId;
    } else if (failedIdx >= 0) {
      setSelectedKey(stages[failedIdx].key);
      autoExpandedFor.current = paymentId;
    }
  }, [stages, states, paymentId]);

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
          {/* The IN/OUT marker, same device as the Activity rows — the deep-dive is
              reached from a list row or the inbound trigger, and both directions land
              here, so the header says which one you are reading. Absent direction =
              outbound (pre-incoming payments carry no field). */}
          <span
            className={`${styles.directionBadge} ${
              payment?.direction === "INBOUND"
                ? styles.directionIn
                : styles.directionOut
            }`}
          >
            {payment?.direction === "INBOUND" ? "INBOUND" : "OUTBOUND"}
          </span>
          {payment?.status && (
            <StatusPill status={payment.status} />
          )}
          <LensToggle lens={lens} onChange={setLens} />
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
        {resolveError && (
          <Banner variant="danger">Could not resolve review — {resolveError}</Banner>
        )}
        {loading && <div className={styles.emptyState}>Loading…</div>}
        {!loading && !error && !payment && (
          <div className={styles.emptyState}>Payment not found: {paymentId}</div>
        )}

        {stages && (
          <>
            <MiniStepper
              stages={stages}
              states={states}
              selectedKey={selectedKey}
              onSelect={setSelectedKey}
            />
            <StagePane
              stage={stages.find((s) => s.key === selectedKey) ?? stages[0]}
              state={states[Math.max(0, stages.findIndex((s) => s.key === selectedKey))]}
              payment={payment}
              trace={trace}
              lens={lens}
              onApprove={() => setStepUpOpen(true)}
              onResolve={resolveReview}
              onResolveException={resolveException}
              onResolveUta={resolveUta}
              onLedgerAction={ledgerAction}
              onAcknowledgeAgent={acknowledgeAgent}
              refreshKey={nudge}
            />
          </>
        )}
      </div>
    </div>
  );
}
