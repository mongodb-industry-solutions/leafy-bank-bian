"use client";

import styles from "./GlDashboardSection.module.css";
import { useGlDashboard } from "@/lib/api/hooks";
import { fmt } from "@/lib/glMonitor/format";

function StatCard({ label, value = "—" }) {
  return (
    <div className={styles.statCard}>
      <span className={styles.statLabel}>{label}</span>
      <span className={styles.statValue}>{value}</span>
    </div>
  );
}

export default function GlDashboardSection({ refreshKey }) {
  // Rolling 3-month window (periodCode null). Amounts are minor units (÷100).
  // The totals are journal-derived, so they only change when the GL batch posts.
  // `refreshKey` (owned by GlPipelineView) advances when the batch actually runs
  // or the user hits Refresh All, forcing a refetch then rather than on a timer.
  const { dashboard, loading } = useGlDashboard(null, true, 5, 3, refreshKey);
  const summary = dashboard?.summary;
  const recon = dashboard?.reconciliation;
  const accounts = dashboard?.topControlAccounts ?? [];
  const ph = loading ? "…" : "—"; // placeholder while fetching vs. no data
  const money = (v) => (v == null ? ph : `$${fmt(v, true)}`);
  // Drop the trailing "— Control" / "- Control" qualifier from control-account names.
  const cleanName = (n) => (n ? n.replace(/\s*[—–-]\s*control\s*$/i, "") : "—");

  return (
    <div className={styles.dashSection}>
      {/* Column 1: Atlas chart */}
      <div className={styles.chartPane}>
        <iframe
          style={{ background: "#FFFFFF", border: "none", borderRadius: 2, boxShadow: "0 2px 10px 0 rgba(70, 76, 79, .2)", width: "100%", height: "100%" }}
          src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=744b8747-2f48-4696-bdbe-b2a683f825e2&maxDataAge=3600&theme=light&autoRefresh=true"
        />
      </div>

      {/* Column 2: Top control accounts table (replaces the bar chart) */}
      <div className={styles.chartPane}>
        <div className={styles.tableWrap}>
          <div className={styles.tableTitle}>Top Control Accounts</div>
          <div className={styles.tableBody}>
          <table className={styles.acctTable}>
            <thead>
              <tr>
                <th className={styles.colCode}>Code</th>
                <th className={styles.colName}>Name</th>
                <th className={styles.num}>Debit</th>
                <th className={styles.num}>Credit</th>
                <th className={styles.num}>Balance</th>
              </tr>
            </thead>
            <tbody>
              {accounts.length === 0 ? (
                <tr>
                  <td colSpan={5} className={styles.empty}>
                    {loading ? "…" : "No data"}
                  </td>
                </tr>
              ) : (
                accounts.map((a) => (
                  <tr key={a.accountCode}>
                    <td>{a.accountCode}</td>
                    <td>{cleanName(a.accountName)}</td>
                    <td className={styles.num}>{fmt(a.debit, true)}</td>
                    <td className={styles.num}>{fmt(a.credit, true)}</td>
                    <td className={styles.num}>{fmt(a.balance, true)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
          </div>
        </div>
      </div>

      {/* Column 3: stat cards */}
      <div className={styles.statsPane}>
        <div className={styles.statsRow}>
          <StatCard
            label="Total Journals"
            value={summary ? summary.totalJournals : ph}
          />
          <StatCard label="Total Debit" value={money(summary?.totalDebit)} />
          <StatCard label="Total Credit" value={money(summary?.totalCredit)} />
        </div>
        <div className={styles.statsRow}>
          <StatCard
            label="Out of Balance"
            value={summary ? summary.outOfBalance : ph}
          />
          <StatCard
            label="Reconciliation"
            value={recon ? recon.status : ph}
          />
        </div>
      </div>
    </div>
  );
}
