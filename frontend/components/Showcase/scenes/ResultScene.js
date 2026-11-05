"use client";

import Link from "next/link";
import Badge from "@leafygreen-ui/badge";
import { Body, Overline } from "@leafygreen-ui/typography";
import { gapOf, minor, railLegOf } from "./format";
import styles from "../Showcase.module.css";

/** The end of the run: the gap before and after, and the outcome. */
export default function ResultScene({ paymentId, scenario, trace, payment, outcome, gapBefore, expectedOutcomes, waiting }) {
  const leg = railLegOf(trace);
  const gapNow = gapOf(leg);
  const reconciled = payment?.lifecycle?.reconciliationStatus === "RECONCILED";

  if (!outcome) {
    return <Body className={styles.muted}>{waiting || "Verifying the result…"}</Body>;
  }
  // R4/R5 phase 1: the request is out and nothing else has changed.
  if (outcome === "ESCALATED") {
    return (
      <div className={styles.section}>
        <div className={styles.findingCard}>
          <Overline>Waiting for {scenario.bank}</Overline>
          <Body>A camt.026 investigation request was sent. The books are untouched until {scenario.bank} answers.</Body>
          <Body className={styles.muted}>Use &quot;Correspondent replies now&quot; above, or wait about 20 seconds.</Body>
        </div>
        <Badge variant="yellow">ESCALATED</Badge>
      </div>
    );
  }
  const resolvedOnly = outcome === "RESOLVED" && !reconciled;
  const unexpected = !expectedOutcomes.includes(outcome);
  return (
    <div className={`${styles.resultBar} ${unexpected ? styles.resultBarWarn : ""}`}>
      <div className={styles.resultHead}>
        <Badge variant="green">
          {outcome === "ANSWERED" ? "CORRESPONDENT REPLIED" : resolvedOnly ? "EXCEPTION RESOLVED" : outcome}
        </Badge>
        {gapBefore != null && (
          <span className={styles.resultGap}>
            Gap <strong>{minor(gapBefore)}</strong> → <strong className={gapNow ? styles.error : undefined}>{minor(gapNow ?? 0)}</strong>
          </span>
        )}
        {paymentId && (
          <Link href={`/payments-workflow?payment=${encodeURIComponent(paymentId)}`} className={styles.resultLink}>
            {resolvedOnly ? "Follow reconciliation in Payments Activity" : "Open in Payments"} →
          </Link>
        )}
      </div>
      <Body className={styles.resultText}>
        {scenario.beats.verify}
        {resolvedOnly && " The ledger marks the payment reconciled on its next batch."}
      </Body>
      {unexpected && (
        <Body className={styles.error}>
          Ended differently than expected ({scenario.expected}). Check the exception and the payment before continuing.
        </Body>
      )}
    </div>
  );
}
