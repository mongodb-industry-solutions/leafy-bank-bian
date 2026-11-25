"use client";

import Badge from "@leafygreen-ui/badge";
import { Body, Overline } from "@leafygreen-ui/typography";
import CountdownRing from "./CountdownRing";
import { PositionDiff } from "./Diff";
import { gapOf, minor, railLegOf } from "./format";
import { OVERDUE_SECONDS } from "../scenarios";
import styles from "../Showcase.module.css";

function ExceptionCard({ exception }) {
  const open = exception.status === "OPEN";
  return (
    <div className={styles.exceptionCard}>
      <Badge variant={open ? "red" : "green"}>{open ? "OPEN" : exception.status}</Badge>
      <Body as="span" className={styles.mono}>{exception.exceptionId}</Body>
      <Body as="span">{exception.category}</Body>
    </div>
  );
}


/** Act 2: the disagreement between our books and the statement is the largest thing on screen. */
export default function ProblemScene({ scenarioKey, paymentId, payment, trace, exceptions, bank, countdown, countdownLeft }) {
  const leg = railLegOf(trace);
  const gap = gapOf(leg);
  const hasStatement = gap != null;
  const waitingForLine = scenarioKey === "TL";

  return (
    <div className={styles.section}>
      {countdown && !exceptions.some((e) => e.status === "OPEN") && countdownLeft > 0 && (
        <CountdownRing left={countdownLeft} total={OVERDUE_SECONDS} label="Statement window" />
      )}
      {hasStatement && (
        <div className={styles.gapRow}>
          <div className={styles.gapSide}>
            <Overline>Our books</Overline>
            <span className={styles.gapFigure}>{minor(leg.leftAmount)}</span>
          </div>
          <div className={styles.gapSide}>
            <Overline>{bank} statement</Overline>
            <span className={styles.gapFigure}>
              {scenarioKey === "R4"
                ? <PositionDiff ours={minor(leg.leftAmount)} theirs={minor(leg.rightAmount)} />
                : minor(leg.rightAmount)}
            </span>
          </div>
          <div className={`${styles.gapSide} ${gap ? styles.gapBad : styles.gapOk}`}>
            <Overline>Gap</Overline>
            <span className={styles.gapFigure}>{minor(gap)}</span>
          </div>
        </div>
      )}
      {!hasStatement && !waitingForLine && !exceptions.length && payment?.lifecycle?.reconciliationStatus !== "RECONCILED" && (
        <Body className={styles.muted}>Waiting for {bank}&apos;s statement.</Body>
      )}
      {scenarioKey === "TL" && !hasStatement && countdownLeft === 0 && (
        <div className={styles.emptyState}>
          <Body>No statement from {bank} yet.</Body>
          <Body className={styles.muted}>The wire settled, but its line has not been reported.</Body>
        </div>
      )}
      {exceptions.map((e) => (
        <ExceptionCard key={e.exceptionId} exception={e} />
      ))}
    </div>
  );
}
