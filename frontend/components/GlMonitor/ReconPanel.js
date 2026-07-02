// Reconciliation panel — ported from recon.js. NOT rendered by GlMonitor (the
// static gl_monitor.html keeps this panel commented out); preserved here so the
// full reference survives and can be dropped back into the layout later.
import React, { useState } from "react";
import styles from "./GlMonitor.module.css";
import { VariantChip } from "./Bits";
import { pipelineApi } from "@/lib/api/client";

export default function ReconPanel({ period = "" }) {
  const [rows, setRows] = useState(null); // null=initial, []=empty, [...]=data
  const [error, setError] = useState(null);
  const [checkedAt, setCheckedAt] = useState("");
  const [running, setRunning] = useState(false);

  async function fetchRecon() {
    setRunning(true);
    setError(null);
    const params = period.trim() ? `?periodCode=${period.trim()}` : "";
    const { data, error: err } = await pipelineApi(`reconciliation${params}`);
    setRunning(false);
    if (err) { setError(err); return; }
    setCheckedAt(new Date().toLocaleTimeString());
    setRows(data || []);
  }

  let body;
  if (running) body = <tr><td colSpan={5} style={{ textAlign: "center", color: "var(--text-xs)" }}>Running…</td></tr>;
  else if (error) body = <tr><td colSpan={5}><div className={styles["error-state"]}>Error: {error}</div></td></tr>;
  else if (rows === null) body = <tr><td colSpan={5} style={{ textAlign: "center", color: "var(--text-xs)", padding: 16 }}>Click &quot;Run Now&quot; to reconcile</td></tr>;
  else if (!rows.length) body = <tr><td colSpan={5} style={{ textAlign: "center", color: "var(--text-xs)" }}>No data</td></tr>;
  else body = rows.map((r, i) => (
    <tr key={i}>
      <td><span style={{ fontFamily: "monospace" }}>{r.accountCode}</span></td>
      <td>{r.periodCode || "—"}</td>
      <td style={{ fontFamily: "monospace" }}>{r.subledgerSum}</td>
      <td style={{ fontFamily: "monospace" }}>{r.journalSum}</td>
      <td>{r.isReconciled
        ? <VariantChip variant="ok">✓ MATCH</VariantChip>
        : <VariantChip variant="mismatch">⚠ MISMATCH</VariantChip>}</td>
    </tr>
  ));

  return (
    <div className={styles.panel}>
      <div className={styles["panel-header"]}>
        <span className={styles["panel-title"]}>Reconciliation</span>
        <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 6 }}>{period.trim() ? `Period: ${period.trim()}` : "(all)"}</span>
        <button className={`${styles.btn} ${styles["btn-sm"]}`} style={{ marginLeft: "auto" }} onClick={fetchRecon}>Run Now</button>
        <span style={{ fontSize: 10, color: "var(--text-xs)", marginLeft: 8 }}>{checkedAt ? `checked ${checkedAt}` : ""}</span>
      </div>
      <div className={styles["panel-body"]}>
        <table className={styles["recon-table"]}>
          <thead>
            <tr>
              <th>Account</th><th>Period</th><th>SL Σ</th><th>JNL Σ</th><th>Status</th>
            </tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
        <div className={styles["col-features"]} style={{ marginTop: 12 }}>
          <span className={styles["col-features-label"]}>MongoDB Features In Use</span>
          <div>
            <span className={styles["feature-tag"]}>$unwind $cond $multiply</span>
            <span className={styles["feature-tag"]}>distinct() control accounts</span>
            <span className={styles["feature-tag"]}>Signed DEBIT+/CREDIT− sums</span>
          </div>
        </div>
      </div>
    </div>
  );
}
