"use client";

import styles from "./GlDashboardSection.module.css";

function StatCard({ label, value = "—" }) {
  return (
    <div className={styles.statCard}>
      <span className={styles.statLabel}>{label}</span>
      <span className={styles.statValue}>{value}</span>
    </div>
  );
}

export default function GlDashboardSection() {
  return (
    <div className={styles.dashSection}>
      {/* Column 1: Atlas chart */}
      <div className={styles.chartPane}>
        <iframe
          style={{ background: "#FFFFFF", border: "none", borderRadius: 2, boxShadow: "0 2px 10px 0 rgba(70, 76, 79, .2)", width: "100%", height: "100%" }}
          src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=744b8747-2f48-4696-bdbe-b2a683f825e2&maxDataAge=3600&theme=light&autoRefresh=true"
        />
      </div>

      {/* Column 2: Atlas chart */}
      <div className={styles.chartPane}>
        <iframe
          style={{ background: "#FFFFFF", border: "none", borderRadius: 2, boxShadow: "0 2px 10px 0 rgba(70, 76, 79, .2)", width: "100%", height: "100%" }}
          src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=8b9b97f7-e1b7-4ace-b506-307a76bae7e9&maxDataAge=3600&theme=light&autoRefresh=true"
        />
      </div>

      {/* Column 3: stat cards */}
      <div className={styles.statsPane}>
        <div className={styles.statsRow}>
          <StatCard label="Total Journals" />
          <StatCard label="Total Debit" />
          <StatCard label="Total Credit" />
        </div>
        <div className={styles.statsRow}>
          <StatCard label="Out of Balance" />
          <StatCard label="Reconciliation" />
        </div>
      </div>
    </div>
  );
}
