"use client";

// The payment lifecycle deep dive — a HORIZONTAL stage rail reading left-to-right in saga
// order, with the selected stage's detail underneath.
//
// Horizontal rather than a vertical spine because the saga is a sequence: reading it
// left-to-right is the point, and a vertical list of nine expandable sections buries the
// shape in scroll. It is also what Doina drew (PaymentTrace.js's own header comment
// describes "a horizontal 6-stage rail"). Putting the detail below the rail instead of
// beside it keeps the rail one line tall no matter how much any stage carries.
//
// Two data sources, composed client-side and never server-side:
//   * /workflow/payments/{id}  (transactions) — stages 1-4
//   * /pipeline/trace/{id}     (ledger)       — stages 5-8
// Neither service reads the other's collections (decisions.md 2026-06-18).
import { Fragment, useEffect, useMemo, useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Banner from "@leafygreen-ui/banner";
import Code from "@leafygreen-ui/code";
import { Tab, Tabs } from "@leafygreen-ui/tabs";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import { Body } from "@leafygreen-ui/typography";

import styles from "./PaymentsWorkflow.module.css";
import { buildLifecycleStages, legTotals } from "./lifecycleStages";
import { usePaymentWorkflow, usePipelineTrace } from "@/lib/api/hooks";
import {
  statusBadgeVariant,
  checkBadgeVariant,
  fmtAmount,
  fmtWhen,
} from "@/lib/paymentsWorkflow/status";

function StageRail({ stages, selectedKey, onSelect }) {
  return (
    <div className={styles.railScroll}>
      <div className={styles.railTrack}>
        {stages.map((s, i) => (
          <Fragment key={s.key}>
            {i > 0 && (
              <span
                className={`${styles.stageConnector} ${
                  stages[i - 1].reached && s.reached ? styles.stageConnectorDone : ""
                }`}
              />
            )}
            <button
              type="button"
              className={`${styles.stageNode} ${
                selectedKey === s.key ? styles.stageNodeActive : ""
              }`}
              onClick={() => onSelect(s.key)}
              disabled={!s.reached}
              aria-pressed={selectedKey === s.key}
              title={s.reached ? s.label : `${s.label} — not reached`}
            >
              <span
                className={`${styles.stageCircle} ${s.reached ? "" : styles.stageCircleEmpty}`}
              >
                <Icon glyph={s.reached ? s.icon : "Minus"} size={18} />
              </span>
              <span className={styles.stageLabel}>{s.label}</span>
              <span className={styles.stageMeta}>{s.meta || "—"}</span>
            </button>
          </Fragment>
        ))}
      </div>
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
            <Badge variant="lightgray">{r.source}</Badge>
          </div>
        </Fragment>
      ))}
    </div>
  );
}

/** A resolved value as one line. Objects (an initiating party, a fee) are summarised. */
function fmtEnriched(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) {
    return value.map((v) => fmtEnriched(v)).join("; ");
  }
  if (typeof value === "object") {
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
        <Badge variant={balanced ? "green" : "red"}>
          {balanced ? "Balanced — DR = CR" : "Out of balance"}
        </Badge>
      </div>
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
            <Badge variant={statusBadgeVariant(e.state)}>{e.state}</Badge>{" "}
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
function Checks({ checks }) {
  if (!checks?.length) {
    return (
      <Body className={styles.muted}>
        No checks recorded for this payment.
      </Body>
    );
  }
  return (
    <div>
      {checks.map((c, i) => {
        const result = c.result || c.outcome;
        const detail = c.detail || c.reason;
        return (
          <div className={styles.check} key={`${c.name || c.checkId}-${i}`}>
            <Badge variant={checkBadgeVariant(result)}>{result || "—"}</Badge>
            <span className={styles.checkLabel}>
              {c.label || c.name || c.checkId || "check"}
              {detail ? <span className={styles.eventReason}> · {detail}</span> : null}
            </span>
            {c.mode && <span className={styles.stageMeta}>{c.mode}</span>}
          </div>
        );
      })}
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
          <Code language="json" copyButtonAppearance="hover">
            {JSON.stringify(iso, null, 2)}
          </Code>
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
            <Code language="xml" copyButtonAppearance="hover">
              {xml}
            </Code>
          </div>
        </Tab>
      )}
    </Tabs>
  );
}


/** Per-kind key/value rows. One place to extend when a later stage lands. */
function summaryRows(stage, payment) {
  const d = stage.data;
  switch (stage.kind) {
    case "initiation":
      return [
        ["Customer", d?.customerId],
        ["Type / Rail", `${d?.type || "—"} · ${d?.rail || "—"}`],
        ["Amount", fmtAmount(d?.amount, d?.currency)],
        ["Priority", d?.priority],
        ["Channel", d?.initiation?.channel],
        ["Debtor", d?.debtor ? `${d.debtor.name || "—"} (${d.debtor.accountId || "—"})` : null],
        ["Creditor", d?.creditor ? `${d.creditor.name || "—"} (${d.creditor.accountId || "external"})` : null],
        ["Remittance", d?.remittance?.unstructured],
        ["Requested execution", d?.requestedExecutionDate],
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
        ["Corridor", payment?.wireDetails?.wireType],
        ["Purpose", payment?.categoryPurpose],
        ["Charges", payment?.fees?.length
          ? payment.fees.map((f) => `${fmtAmount(f.amount, f.currency)} ${f.type} (${f.chargedTo})`).join(", ")
          : null],
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
    default:
      return [["Current state", payment?.lifecycle?.currentState]];
  }
}

function StageDetail({ stage, payment }) {
  if (!stage) return null;

  if (!stage.reached) {
    return (
      <div className={styles.stageDetail}>
        <Body className={styles.muted}>
          {stage.label} — not reached yet.
        </Body>
      </div>
    );
  }

  const showLegs = !!stage.legs;
  const showEnrichment = stage.kind === "enrichment";
  const showAuthorization = stage.kind === "authorization";
  const showRailExecution = stage.kind === "railExecution";
  // Stages 3 and 4 render checks too, but their `data` is an object rather than the bare
  // array stage 2 passes, so the shapes are resolved separately.
  const showChecks =
    stage.kind === "checks" || showEnrichment || showAuthorization || showRailExecution;
  const showStates = stage.kind === "states";
  const checkList =
    showEnrichment || showAuthorization || showRailExecution
      ? stage.data?.checks
      : stage.data;

  return (
    <div className={styles.stageDetail}>
      <div className={styles.stageDetailHead}>
        <Icon glyph={stage.icon} size={16} />
        <span className={styles.panelTitle}>
          {/* Doina's stage 6 (Accounting/Posting) has three panels — ledger event,
              sub-ledger, general ledger — so the group name disambiguates them without
              inventing stage numbers she does not have (doc 20 B4). */}
          Stage {stage.stage} · {stage.group ? `${stage.group} — ${stage.label}` : stage.label}
        </span>
        {stage.status && (
          <Badge variant={statusBadgeVariant(stage.status)}>{stage.status}</Badge>
        )}
      </div>

      <div className={styles.detailColumns}>
        {showChecks && (
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
        {!showStates && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>Summary</div>
            <KeyValues rows={summaryRows(stage, payment)} />
          </div>
        )}

        {showEnrichment && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>Progressive enrichment</div>
            <EnrichmentDiff enrichment={stage.data?.enrichment} />
          </div>
        )}

        {showRailExecution && (
          <div className={styles.detailBlockWide}>
            <div className={styles.detailBlockTitle}>
              Canonical payment &rarr; ISO 20022
            </div>
            <RailViews data={stage.data} />
          </div>
        )}

        {(showEnrichment || showAuthorization || showRailExecution) && !!stage.data?.events?.length && (
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

        {stage.data && !showChecks && !showStates && !showEnrichment && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>Raw document</div>
            <Code language="json" copyButtonAppearance="hover">
              {JSON.stringify(stage.data, null, 2)}
            </Code>
          </div>
        )}
      </div>
    </div>
  );
}

export default function PaymentDeepDive({ paymentId, refreshKey, onBack }) {
  const { payment, loading, error } = usePaymentWorkflow(paymentId, refreshKey);
  // Ledger half. Self-terminating poll — stops once the journal entry lands.
  const { trace } = usePipelineTrace(paymentId, !!paymentId);
  const [selectedKey, setSelectedKey] = useState("initiation");

  // Reset to the first stage when a different payment is opened — keyed on paymentId, not
  // on the trace, so the 2s re-polls don't clobber the current selection every tick.
  useEffect(() => {
    setSelectedKey("initiation");
  }, [paymentId]);

  const stages = useMemo(
    () => (payment ? buildLifecycleStages(payment, trace) : null),
    [payment, trace]
  );
  const selected = stages?.find((s) => s.key === selectedKey) || null;

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
        <Icon glyph="Diagram3" size={16} />
        <span className={styles.panelTitle}>Payment lifecycle</span>
        <span className={`${styles.mono} ${styles.muted}`}>{paymentId}</span>
        {payment?.status && (
          <Badge variant={statusBadgeVariant(payment.status)}>{payment.status}</Badge>
        )}
      </div>

      <div className={styles.panelBody}>
        {error && <Banner variant="danger">Could not load payment — {error}</Banner>}
        {loading && <div className={styles.emptyState}>Loading…</div>}
        {!loading && !error && !payment && (
          <div className={styles.emptyState}>Payment not found: {paymentId}</div>
        )}

        {stages && (
          <>
            <StageRail stages={stages} selectedKey={selectedKey} onSelect={setSelectedKey} />
            <StageDetail stage={selected} payment={payment} />
          </>
        )}
      </div>
    </div>
  );
}
