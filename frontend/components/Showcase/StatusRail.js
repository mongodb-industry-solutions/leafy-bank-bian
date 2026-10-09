"use client";

// Sticky right rail: who the payment is, where it stands, and (collapsed) what has been
// written to MongoDB so far.

import Badge from "@leafygreen-ui/badge";
import { H3, Body } from "@leafygreen-ui/typography";
import StepDocuments from "./StepDocuments";
import LiveLog from "./LiveLog";
import ScenarioInfo from "./ScenarioBrief";
import styles from "./Showcase.module.css";

const chip = (label, ok) => <Badge key={label} variant={ok ? "green" : "lightgray"}>{label}</Badge>;

function exceptionChip(exceptions) {
  if (!exceptions.length) return null;
  const open = exceptions.filter((e) => e.status === "OPEN");
  if (!open.length) return <Badge variant="green">Exception closed</Badge>;
  const waiting = open.every((e) => e.awaitingCounterparty);
  return (
    <Badge variant={waiting ? "yellow" : "red"}>
      {waiting ? "Awaiting correspondent" : `${open.length} exception${open.length === 1 ? "" : "s"} open`}
    </Badge>
  );
}

// `chips` replaces the reconciliation chips and the documents panel (the cut-off walkthrough).
export default function StatusRail({ scenario, init, payment, exceptions, docs, chips, events }) {
  const amount = Number(init?.amount ?? payment?.amount ?? 0);
  return (
    <aside className={styles.rail}>
      <div className={`${styles.railCard} ${styles.toneBorder}`}>
        <H3 as="p" className={styles.paneTitle}>
          {init ? `${payment?.currency || "USD"} ${amount.toLocaleString(undefined, { minimumFractionDigits: 2 })}` : `USD ${scenario.amount}`}
        </H3>
        <div className={styles.railTitleRow}>
          <Body className={styles.muted}>
            {scenario.bank} · {scenario.title}
          </Body>
          <ScenarioInfo scenario={scenario} />
        </div>
        {init ? (
          <Body className={styles.mono}>{init.paymentId}</Body>
        ) : (
          <Body className={styles.muted}>Not sent yet.</Body>
        )}
        {init && chips && <div className={styles.chipRow}>{chips}</div>}
        {init && !chips && (
          <div className={styles.chipRow}>
            {chip("Settled", ["SETTLED", "COMPLETED"].includes(payment?.status))}
            {chip("Booked", payment?.lifecycle?.postingStatus === "POSTED")}
            {chip("Reconciled", payment?.lifecycle?.reconciliationStatus === "RECONCILED")}
            {exceptionChip(exceptions)}
          </div>
        )}
      </div>

      {events && <LiveLog events={events} sources={docs?.sources} />}

      {!chips && !events && (
        <details className={styles.railDocs}>
          <summary className={styles.railDocsSummary}>Written to MongoDB</summary>
          <StepDocuments {...docs} />
        </details>
      )}
    </aside>
  );
}
