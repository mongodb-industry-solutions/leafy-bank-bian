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

function Checks({ checks }) {
  if (!checks?.length) {
    return (
      <Body className={styles.muted}>
        No checks recorded — entitlement checks arrive with stage 2.
      </Body>
    );
  }
  return (
    <div>
      {checks.map((c, i) => (
        <div className={styles.check} key={`${c.name || c.checkId}-${i}`}>
          <Badge variant={checkBadgeVariant(c.outcome)}>{c.outcome || "—"}</Badge>
          <span className={styles.checkLabel}>
            {c.label || c.name || c.checkId || "check"}
            {c.reason ? <span className={styles.eventReason}> · {c.reason}</span> : null}
          </span>
          {c.mode && <span className={styles.stageMeta}>{c.mode}</span>}
        </div>
      ))}
    </div>
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
  const showChecks = stage.kind === "checks";
  const showStates = stage.kind === "states";

  return (
    <div className={styles.stageDetail}>
      <div className={styles.stageDetailHead}>
        <Icon glyph={stage.icon} size={16} />
        <span className={styles.panelTitle}>
          Stage {stage.stage} · {stage.label}
        </span>
        {stage.status && (
          <Badge variant={statusBadgeVariant(stage.status)}>{stage.status}</Badge>
        )}
      </div>

      <div className={styles.detailColumns}>
        {showChecks && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>Checks</div>
            <Checks checks={stage.data} />
          </div>
        )}

        {showStates && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>State transitions</div>
            <StateEvents events={stage.data} />
          </div>
        )}

        {!showChecks && !showStates && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>Summary</div>
            <KeyValues rows={summaryRows(stage, payment)} />
          </div>
        )}

        {showLegs && (
          <div className={styles.detailBlock}>
            <div className={styles.detailBlockTitle}>Double-entry</div>
            <Legs legs={stage.legs} />
          </div>
        )}

        {stage.data && !showChecks && !showStates && (
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
