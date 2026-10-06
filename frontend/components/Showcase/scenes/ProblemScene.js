"use client";

import Badge from "@leafygreen-ui/badge";
import { Body, Overline } from "@leafygreen-ui/typography";
import CountdownRing from "./CountdownRing";
import { PositionDiff, ReferenceDiff } from "./Diff";
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

const statementEntries = (payment) => (payment?.statements || []).flatMap((s) => s.entries || []);

/** R2: our reference against the one the correspondent wrote on the same payment's line. */
function ReferencePair({ paymentId, payment }) {
  const line = statementEntries(payment).find((e) => e.simulatedPaymentId === paymentId);
  if (!line) return null;
  return (
    <div className={styles.gapRow}>
      <div className={styles.gapSide}>
        <Overline>Our reference</Overline>
        <span className={`${styles.gapFigure} ${styles.mono}`}>
          <ReferenceDiff ours={paymentId} theirs={line.reference} side="ours" />
        </span>
      </div>
      <div className={`${styles.gapSide} ${styles.gapBad}`}>
        <Overline>On the statement</Overline>
        <span className={`${styles.gapFigure} ${styles.mono}`}>
          <ReferenceDiff ours={paymentId} theirs={line.reference} side="theirs" />
        </span>
      </div>
    </div>
  );
}

/** R5: every line on the statement, with the one that belongs to no payment flagged. */
function StatementLines({ payment }) {
  const entries = statementEntries(payment);
  if (!entries.length) return null;
  return (
    <table className={styles.linesTable}>
      <thead>
        <tr><th>Reference</th><th>Amount</th><th>Payment</th></tr>
      </thead>
      <tbody>
        {entries.map((e) => (
          <tr key={`${e.reference}-${e.lineNo}`} className={e.simulatedPaymentId ? "" : styles.lineOrphan}>
            <td className={styles.mono}>{e.reference}</td>
            <td>{Number(e.amount).toLocaleString(undefined, { minimumFractionDigits: 2 })}</td>
            <td>{e.simulatedPaymentId ? "matched to ours" : "no payment"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Act 2: the disagreement between our books and the statement is the largest thing on screen. */
export default function ProblemScene({ scenarioKey, paymentId, payment, trace, exceptions, bank, countdown, countdownLeft }) {
  const leg = railLegOf(trace);
  const gap = gapOf(leg);
  const hasStatement = gap != null;
  const waitingForLine = scenarioKey === "TL" || scenarioKey === "R2";

  return (
    <div className={styles.section}>
      {countdown && !exceptions.some((e) => e.status === "OPEN") && countdownLeft > 0 && (
        <CountdownRing left={countdownLeft} total={OVERDUE_SECONDS} label="Statement window" />
      )}
      {scenarioKey === "R2" && <ReferencePair paymentId={paymentId} payment={payment} />}
      {scenarioKey === "R5" && <StatementLines payment={payment} />}
      {hasStatement && scenarioKey !== "R2" && scenarioKey !== "R5" && (
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
      {!hasStatement && !waitingForLine && (
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
