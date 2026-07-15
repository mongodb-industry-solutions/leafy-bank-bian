import React from "react";
import styles from "./GlMonitor.module.css";

const STEPS = [
  { cls: "step-tx", n: "0", title: "Transactions" },
  { cls: "step-le", n: "1", title: "Ledger Events" },
  { cls: "step-sl", n: "2", title: "SubLedger Entries" },
  { cls: "step-jn", n: "3", title: "Journal Entries" },
];

export default function PipelineStepper() {
  return (
    <div className={styles["pipeline-stepper"]}>
      <div className={styles["stepper-inner"]}>
        {STEPS.map((s) => (
          <div className={styles["stepper-step"]} key={s.cls}>
            <div className={`${styles["stepper-circle"]} ${styles[s.cls]}`}>{s.n}</div>
            <div className={styles["stepper-title"]}>{s.title}</div>
          </div>
        ))}

        {/* Hop labels marking the processing mechanism between stages. Both
            0→1 (ingest_worker) and 1→2 (projection_worker) are async, change-
            stream-driven; 2→3 (gl_batch) is a scheduled batch. */}
        <div className={`${styles["stepper-flow-label"]} ${styles["stepper-flow-label-01"]}`}>
          <span className={styles["live-dot"]} />
          change stream
        </div>
        <div className={`${styles["stepper-flow-label"]} ${styles["stepper-flow-label-12"]}`}>
          <span className={styles["live-dot"]} />
          change stream
        </div>
        <div className={`${styles["stepper-flow-label"]} ${styles["stepper-flow-label-23"]}`}>
          <span className={styles["stepper-batch-icon"]}>⏱</span>
          scheduled batch
        </div>
      </div>
    </div>
  );
}
