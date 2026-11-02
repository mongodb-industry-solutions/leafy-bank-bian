"use client";

// The payment lifecycle as a pipeline: the eight stages, with the wire's position lit. The
// cut-off scenarios hold a wire at stages 2 to 4, so the presenter can see which stage it is
// stuck in and when it moves on. The reconciliation scenarios run stages 1 to 7 normally and
// then sit at stage 8 until the books and the statement agree (`mode="reconciliation"`).
// Position comes from `lifecycle.currentState`.

import Link from "next/link";
import Icon from "@leafygreen-ui/icon";
import { Body, Overline } from "@leafygreen-ui/typography";
import { STATE_INFO } from "./cutoffRunLog";
import styles from "./Showcase.module.css";

const STAGES = [
  { n: 1, label: "Initiation" },
  { n: 2, label: "Authentication & Authorization" },
  { n: 3, label: "Validate & Enrich" },
  { n: 4, label: "Orchestration & Approval" },
  { n: 5, label: "Payment Execution" },
  { n: 6, label: "Accounting & Posting" },
  { n: 7, label: "Clearing & Settlement" },
  { n: 8, label: "Reconciliation" },
];

// [stage the wire is in, held?]. The stage is where the wire is now, so every earlier stage is
// done. SETTLED sits at stage 8 because stage 7 is finished and reconciliation is next.
const POSITION = {
  DRAFT: [1], INITIATED: [2], PENDING_APPROVAL: [2, true],
  VALIDATED: [3], ENRICHED: [3], PENDING_FUNDS: [3, true],
  FINAL_VALIDATED: [4], ROUTED: [4], AUTHORISED: [4], APPROVED: [4],
  PENDING_SCREENING: [4, true], CUTOFF_EXCEPTION: [4, true], MANUAL_FRAUD_REVIEW: [4, true],
  SUBMITTED: [5], IN_PROGRESS: [6], POSTED: [7], SETTLED: [8], RECONCILED: [8],
};

// A deferred wire is back at ROUTED with a later value date: parked on purpose, not in flight.
const DEFERRED = {
  DEFER_NEXT_BUSINESS_DAY: "Deferred to the next business day. The wire waits at routing.",
  HOLD_NEXT_VALUE_DATE: "Held for the next value date. The wire waits at routing.",
};

const RECON_NOTES = {
  held: "The books and the correspondent's statement disagree. The reconciliation agent is investigating.",
  open: "Settled. Waiting to match the correspondent's statement against our books.",
};

// Reconciliation mode: stage 8 is the live stage. It is held while an exception is open, and
// complete once the payment's own reconciliation closes.
function locateRecon(payment, status, { reconciled, exceptionsOpen }) {
  const state = payment?.lifecycle?.currentState || status;
  if (!state) return { stage: 0, held: false, note: "Not sent yet" };
  const complete = !!reconciled || state === "RECONCILED";
  const stage = complete ? 8 : state === "COMPLETED" ? 8 : (POSITION[state] || [0])[0];
  const held = !complete && stage === 8 && !!exceptionsOpen;
  const info = STATE_INFO[state];
  return {
    state, stage, held, complete, failed: !stage,
    note: held ? RECON_NOTES.held
      : stage === 8 && !complete ? RECON_NOTES.open
      : info?.why || info?.title || state.replaceAll("_", " ").toLowerCase(),
  };
}

function locate(payment, status) {
  const state = payment?.lifecycle?.currentState || status;
  if (!state) return { stage: 0, held: false, note: "Not sent yet" };
  const deferred = state === "ROUTED" && DEFERRED[payment?.cutoff?.decision];
  const [stage, held] = POSITION[state] || [0, false];
  const info = STATE_INFO[state];
  return {
    state,
    stage: deferred ? 4 : stage,
    held: !!held || !!deferred,
    complete: state === "RECONCILED",
    failed: !POSITION[state],
    note: deferred || info?.why || info?.title || state.replaceAll("_", " ").toLowerCase(),
  };
}

// From SETTLED on, the showcase has nothing left to show: the money moved. Reconciliation (stage 8)
// plays out over minutes on the statement cycle, so it is handed to the Payments page.
function handedOff(where) {
  return where.state === "SETTLED" || where.state === "RECONCILED";
}

function nodeStatus(n, where, recon) {
  if (!recon && handedOff(where) && n === 8) return "next";
  if (where.complete) return "done";
  if (n < where.stage) return "done";
  if (n === where.stage) return where.held ? "held" : "now";
  return "idle";
}

export default function PaymentPipeline({ payment, status, paymentId, mode = "cutoff", reconciled, exceptionsOpen }) {
  const recon = mode === "reconciliation";
  const where = recon ? locateRecon(payment, status, { reconciled, exceptionsOpen }) : locate(payment, status);
  const handoff = !recon && handedOff(where);
  const current = STAGES.find((s) => s.n === where.stage);
  return (
    <div className={styles.pipeline}>
      <div className={styles.pipelineHead}>
        <Overline>Payment lifecycle</Overline>
        <Body as="span" className={`${styles.pipelineNow} ${!where.state ? styles.pipelineIdle : ""}`}>
          {where.failed
            ? "Stopped before completing"
            : where.complete && recon
              ? "Reconciled"
              : handoff
              ? "Stages 1 to 7 complete"
              : current
                ? `Stage ${current.n} · ${current.label}`
                : "Starts when the wire is sent"}
        </Body>
        {where.state && !handoff && <span className={styles.pipelineState}>{where.state}</span>}
      </div>

      <ol className={`${styles.pipelineTrack} ${!where.state ? styles.pipelineTrackIdle : ""}`} aria-label="Payment lifecycle stages">
        {STAGES.map((s) => {
          const st = nodeStatus(s.n, where, recon);
          return (
            <li key={s.n} className={`${styles.pipeStage} ${styles[`pipe_${st}`]}`} aria-current={st === "now" || st === "held" ? "step" : undefined}>
              <span className={styles.pipeNode}>
                {st === "done" ? <Icon glyph="Checkmark" size={14} /> : st === "next" ? <Icon glyph="ArrowRight" size={14} /> : st === "held" ? <Icon glyph="Clock" size={14} /> : s.n}
              </span>
              <span className={styles.pipeLabel}>{s.label}</span>
              {st === "held" && <span className={styles.pipeAgent}><Icon glyph="Sparkle" size={10} /> {recon ? "Reconciliation agent" : "Cut-off agent"}</span>}
            </li>
          );
        })}
      </ol>

      {handoff && (
        <div className={styles.pipelineHandoff}>
          <Icon glyph="CheckmarkWithCircle" size={16} />
          <Body as="span" className={styles.pipelineHandoffText}>
            <strong>Funds settled.</strong> Reconciliation against the correspondent statement runs next, outside this scenario.
          </Body>
          {paymentId && (
            <Link href={`/payments-workflow?payment=${encodeURIComponent(paymentId)}`} className={styles.pipelineHandoffLink}>
              Follow reconciliation in Payments <Icon glyph="ArrowRight" size={12} />
            </Link>
          )}
        </div>
      )}

      {where.state && !handoff && (
        <Body className={`${styles.pipelineNote} ${where.held ? styles.pipelineNoteHeld : ""}`}>
          {where.held ? `Held at stage ${where.stage}. ${where.note}` : where.failed ? `Payment ended as ${where.note}.` : where.note}
        </Body>
      )}
    </div>
  );
}
