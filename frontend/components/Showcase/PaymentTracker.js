"use client";

import Badge from "@leafygreen-ui/badge";
import { H3, Body, Overline } from "@leafygreen-ui/typography";
import { buildLifecycleStages } from "@/components/PaymentsWorkflow/lifecycleStages";
import styles from "./Showcase.module.css";

const GOOD = new Set(["COMPLETED", "POSTED", "SETTLED", "RECONCILED", "MATCH", "ACCEPTED", "APPROVED", "RESOLVED"]);
const BAD = new Set(["FAILED", "REJECTED", "DISCREPANT", "MISMATCH", "OPEN", "DECLINED"]);

function badgeVariant(status, reached) {
  if (!reached) return "lightgray";
  if (GOOD.has(status)) return "green";
  if (BAD.has(status)) return "red";
  return "yellow";
}

const money = (minor) =>
  minor == null ? "—" : (Math.abs(Number(minor)) / 100).toLocaleString(undefined, { minimumFractionDigits: 2 });

export default function PaymentTracker({ init, payment, trace, exceptions }) {
  if (!init) {
    return (
      <div className={styles.pane}>
        <H3 className={styles.paneTitle}>Payment</H3>
        <Body className={styles.muted}>Click Next to initiate the scenario's wire.</Body>
      </div>
    );
  }

  const stages = payment ? buildLifecycleStages(payment, trace) : [];
  const railLeg = (trace?.reconciliation?.legs || []).find((l) => l.leg === "RAIL_SETTLEMENT");
  // Assumption: the -ADJ correction appears in allLedgerEvents once the trace includes it.
  const adj = (trace?.allLedgerEvents || []).find((e) => String(e?.idempotencyKey || "").endsWith("-ADJ"));

  return (
    <div className={styles.pane}>
      <H3 className={styles.paneTitle}>Payment</H3>
      <div className={styles.paymentHead}>
        <span className={styles.mono}>{init.paymentId}</span>
        <span className={styles.amount}>
          {payment?.currency || "USD"} {Number(init.amount ?? payment?.amount ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}
        </span>
        <span className={styles.muted}>
          bearer {init.chargeBearer || payment?.chargeBearer || "—"} · {init.bic || payment?.creditor?.bic || "—"}
          {init.bankName ? ` · ${init.bankName}` : ""}
        </span>
      </div>

      <ol className={styles.stageList}>
        {stages.map((s) => (
          <li key={s.key} className={styles.stageRow}>
            <span className={styles.stageNum}>{s.stage}</span>
            <span className={styles.stageLabel}>{s.label}</span>
            <span className={styles.stageMeta}>{s.reached ? s.meta : ""}</span>
            <Badge variant={badgeVariant(s.status, s.reached)}>
              {s.reached ? s.status || "reached" : "—"}
            </Badge>
          </li>
        ))}
      </ol>

      {railLeg && railLeg.rightAmount != null && (
        <div className={styles.section}>
          <Overline>Statement line vs posted</Overline>
          <Body className={styles.factRow}>
            Posted {money(railLeg.leftAmount)} · Statement {money(railLeg.rightAmount)}{" "}
            <Badge variant={badgeVariant(railLeg.result, true)}>{railLeg.result}</Badge>
          </Body>
        </div>
      )}

      {exceptions.length > 0 && (
        <div className={styles.section}>
          <Overline>Exceptions</Overline>
          {exceptions.map((e) => (
            <Body key={e.exceptionId} className={styles.factRow}>
              <span className={styles.mono}>{e.exceptionId}</span> · {e.category}{" "}
              <Badge variant={e.status === "OPEN" ? (e.awaitingCounterparty ? "yellow" : "red") : "green"}>
                {e.awaitingCounterparty && e.status === "OPEN" ? "AWAITING COUNTERPARTY" : e.status}
              </Badge>
            </Body>
          ))}
        </div>
      )}

      {adj && (
        <div className={styles.section}>
          <Overline>Correcting GL legs</Overline>
          <Body className={styles.factRow}>
            Dr {adj.debitLeg?.glAccountCode} / Cr {adj.creditLeg?.glAccountCode} ·{" "}
            {money(adj.debitLeg?.amount)}{" "}
            <Badge variant={badgeVariant(adj.postingStatus, true)}>{adj.postingStatus}</Badge>
          </Body>
        </div>
      )}
    </div>
  );
}
