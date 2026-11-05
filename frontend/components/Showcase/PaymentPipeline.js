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
import { POSITION, STAGES, STAGE_STATE, stageOfState } from "./lifecycleStages";
import styles from "./Showcase.module.css";

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
//
// With `paced`, the strip follows the run log instead of the live data, which runs seconds
// ahead of it. `paced.ceiling` is the furthest stage whose log line has been shown, and `held` /
// `complete` switch only once the log has shown the exception opening and the payment
// reconciling, so each tick lands with its line. An unknown state still shows as failed.
function locateRecon(payment, status, { reconciled, exceptionsOpen }, paced) {
  const liveState = payment?.lifecycle?.currentState || status;
  if (!liveState) return { stage: 0, held: false, note: "Not sent yet" };
  const liveComplete = !!reconciled || liveState === "RECONCILED";
  const liveStage = liveComplete ? 8 : stageOfState(liveState);
  const complete = paced ? liveComplete && !!paced.complete : liveComplete;
  const stage = !paced || !liveStage ? liveStage : complete ? 8 : paced.ceiling;
  if (paced && liveStage && stage === 0) return { stage: 0, held: false, note: "Not sent yet" };
  // A stage the live payment has moved past (or not yet reached) shows its own representative state.
  const state = stage === liveStage ? liveState : STAGE_STATE[stage];
  const held = !complete && stage === 8 && (paced ? !!paced.held : !!exceptionsOpen);
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

export default function PaymentPipeline({ payment, status, paymentId, mode = "cutoff", reconciled, exceptionsOpen, paced }) {
  const recon = mode === "reconciliation";
  const where = recon ? locateRecon(payment, status, { reconciled, exceptionsOpen }, paced) : locate(payment, status);
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

      {where.state && !handoff && !(recon && where.complete) && (
        <Body className={`${styles.pipelineNote} ${where.held ? styles.pipelineNoteHeld : ""}`}>
          {where.held ? `Held at stage ${where.stage}. ${where.note}` : where.failed ? `Payment ended as ${where.note}.` : where.note}
        </Body>
      )}
    </div>
  );
}
