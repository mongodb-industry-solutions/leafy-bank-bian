import React from "react";
import styles from "./GlMonitor.module.css";
import { fmtDate } from "@/lib/glMonitor/format";

// health: the raw /pipeline/health payload (or null). statusText mirrors the
// original #health-status line ("updated …" / "error: …" / "checking…").
export default function HealthBar({ health, statusText }) {
  const d = health || {};
  const batch = d.batchIntervalSeconds ? `every ${d.batchIntervalSeconds}s` : "—";
  const last = d.lastBatchAt ? ` · last ${fmtDate(d.lastBatchAt)}` : "";

  return (
    <div className={styles.healthbar}>
      <div className={styles["health-group"]}>
        <div className={styles["health-label"]}>Ledger Events</div>
        <div className={styles["health-stats"]}>
          <span className={`${styles.hstat} ${styles["hstat-pending"]}`}>{d.ledgerEvents?.pending ?? 0} pending</span>
          <span className={`${styles.hstat} ${styles["hstat-posted"]}`}>{d.ledgerEvents?.posted ?? 0} posted</span>
          <span className={`${styles.hstat} ${styles["hstat-failed"]}`}>{d.ledgerEvents?.failed ?? 0} failed</span>
        </div>
      </div>
      <div className={styles["health-div"]} />
      <div className={styles["health-group"]}>
        <div className={styles["health-label"]}>SubLedger</div>
        <div className={styles["health-stats"]}>
          <span className={`${styles.hstat} ${styles["hstat-pending"]}`}>{d.subLedger?.unjournaled ?? 0} unjournaled</span>
          <span className={`${styles.hstat} ${styles["hstat-posted"]}`}>{d.subLedger?.journaled ?? 0} journaled</span>
        </div>
      </div>
      <div className={styles["health-div"]} />
      <div className={styles["health-group"]}>
        <div className={styles["health-label"]}>Journals</div>
        <div className={styles["health-stats"]}>
          <span className={`${styles.hstat} ${styles["hstat-posted"]}`}>{d.journals?.total ?? 0} total</span>
          <span className={`${styles.hstat} ${styles["hstat-neutral"]}`}>{d.journals?.thisPeriod ?? 0} this period</span>
        </div>
      </div>
      <div className={styles["health-div"]} />
      <div className={styles["health-batch"]}>{`Batch: ${batch}${last}`}</div>
      <div className={styles.healthStatus}>{statusText}</div>
    </div>
  );
}
