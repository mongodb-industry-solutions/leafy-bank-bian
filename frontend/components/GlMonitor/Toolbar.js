import React from "react";
import styles from "./GlMonitor.module.css";

export default function Toolbar({ period, status, onPeriodChange, onStatusChange, onRefresh }) {
  return (
    <div className={styles.toolbar}>
      <label>Period</label>
      <input
        type="text"
        placeholder="2026-06"
        style={{ width: 90 }}
        value={period}
        onChange={(e) => onPeriodChange(e.target.value)}
      />
      <label>Status (events)</label>
      <select value={status} onChange={(e) => onStatusChange(e.target.value)}>
        <option value="">ALL</option>
        <option value="PENDING">PENDING</option>
        <option value="POSTED">POSTED</option>
        <option value="FAILED">FAILED</option>
      </select>
      <button className={styles.btn} onClick={onRefresh}>↻ Refresh All</button>
      <button className={`${styles.btn} ${styles["btn-primary"]}`} style={{ paddingTop: 5, paddingBottom: 5 }} onClick={onRefresh}>Load Data</button>
    </div>
  );
}
