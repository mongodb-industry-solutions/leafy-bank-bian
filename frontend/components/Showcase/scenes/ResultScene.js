"use client";

import Link from "next/link";
import Badge from "@leafygreen-ui/badge";
import { Body, Overline } from "@leafygreen-ui/typography";
import { gapOf, minor, railLegOf } from "./format";
import styles from "../Showcase.module.css";

/** Act 5: before and after, and the artefact written (or "nothing written, by design"). */
export default function ResultScene({ paymentId, proposal, scenario, trace, payment, outcome, gapBefore, expectedOutcomes, waiting }) {
  const leg = railLegOf(trace);
  const gapNow = gapOf(leg);
  // The correcting journal is the only artefact the trace exposes; absent means none was booked.
  const adj = (trace?.allLedgerEvents || []).find((e) => String(e?.idempotencyKey || "").endsWith("-ADJ"));
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
  const target = proposal?.params?.target;
  const written = adj ? (
    <Body>
      Journal Dr {adj.debitLeg?.glAccountCode} / Cr {adj.creditLeg?.glAccountCode} ·{" "}
      {minor(adj.debitLeg?.amount)} · {adj.postingStatus}
    </Body>
  ) : target ? (
    <Body>Statement line {target.reference} linked to {paymentId}. Both exceptions closed; no journal.</Body>
  ) : (
    <Body>{scenario.key === "TL" ? "Nothing booked. The recheck found the line." : "No journal written, by design."}</Body>
  );

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
      <div className={styles.findingCard}>
        <Overline>Written</Overline>
        {written}
      </div>
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
