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
  return (
    <div className={styles.section}>
      {gapBefore != null && (
        <div className={styles.gapRow}>
          <div className={`${styles.gapSide} ${styles.gapBad}`}>
            <Overline>Gap before</Overline>
            <span className={styles.gapFigure}>{minor(gapBefore)}</span>
          </div>
          <div className={`${styles.gapSide} ${gapNow ? styles.gapBad : styles.gapOk}`}>
            <Overline>Gap now</Overline>
            <span className={styles.gapFigure}>{minor(gapNow ?? 0)}</span>
          </div>
        </div>
      )}
      <div className={styles.chipRow}>
        <Badge variant={outcome === "ESCALATED" ? "yellow" : "green"}>
          {outcome === "ANSWERED" ? "CORRESPONDENT REPLIED" : outcome}
        </Badge>
        {reconciled && <Badge variant="green">Payment reconciled</Badge>}
      </div>
      <Body>{scenario.beats.verify}</Body>
      {paymentId && (
        <Body>
          <Link href={`/payments-workflow?payment=${encodeURIComponent(paymentId)}`}>
            Open the full lifecycle in Payments →
          </Link>
        </Body>
      )}
      <Body className={styles.muted}>Expected: {scenario.expected}</Body>
      {!expectedOutcomes.includes(outcome) && (
        <Body className={styles.error}>
          This run ended differently than the scenario expects. Check the exception and the
          payment before continuing.
        </Body>
      )}
    </div>
  );
}
