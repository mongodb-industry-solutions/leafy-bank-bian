"use client";

import styles from "./GlDashboardSection.module.css";
import { useGlDashboard } from "@/lib/api/hooks";
import { fmt } from "@/lib/glMonitor/format";

// "2026-05".."2026-07" → "May – Jul 2026"; single month → "Jul 2026".
function periodRangeLabel(periods) {
  if (!periods || periods.length === 0) return "";
  const label = (p) => {
    const [y, m] = p.split("-");
    const d = new Date(Date.UTC(Number(y), Number(m) - 1, 1));
    return { mon: d.toLocaleString("en-US", { month: "short", timeZone: "UTC" }), y };
  };
  const first = label(periods[0]);
  const last = label(periods[periods.length - 1]);
  if (periods.length === 1) return `${first.mon} ${first.y}`;
  return first.y === last.y
    ? `${first.mon} – ${last.mon} ${last.y}`
    : `${first.mon} ${first.y} – ${last.mon} ${last.y}`;
}

// status: "good" | "bad" | undefined — tints the card to surface health at a glance.
function StatCard({ label, value = "—", status }) {
  const statusCls = status === "good" ? styles.statGood : status === "bad" ? styles.statBad : "";
  return (
    <div className={`${styles.statCard} ${statusCls}`}>
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
  // Balance = Debit − Credit; render negatives with a leading minus sign.
  const balanceCell = (v) => {
    if (v == null) return ph;
    const abs = fmt(Math.abs(v), true);
    return v < 0 ? `-${abs}` : abs;
  };
  // These blocks are a rolling 3-month roll-up (not a single batch). Label the
  // window from the periods the API actually aggregated so it reads unambiguously.
  const scopeLabel = periodRangeLabel(dashboard?.periods);
  // Totals across the accounts shown — nets to ~0 when this slice is balanced.
  const totalDebit = accounts.reduce((sum, a) => sum + (a.debit || 0), 0);
  const totalCredit = accounts.reduce((sum, a) => sum + (a.credit || 0), 0);
  const totalBalance = totalDebit - totalCredit;

  return (
    <div className={styles.dashSection}>
      {/* Column 1: Atlas chart */}
      <div className={styles.chartPane}>
        <iframe
          style={{ background: "#FFFFFF", border: "none", borderRadius: 2, boxShadow: "0 2px 10px 0 rgba(70, 76, 79, .2)", width: "100%", height: "100%" }}
          src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=9efe5dfd-d969-406d-a03f-35b2ca6f65e7&maxDataAge=60&theme=light&autoRefresh=true"
        />
      </div>

      {/* Column 2: Top control accounts table (replaces the bar chart) */}
      <div className={styles.chartPane}>
        <div className={styles.tableWrap}>
          <div className={styles.tableHead}>
            <div className={styles.tableTitle}>Top Control Accounts</div>
            {scopeLabel && <span className={styles.scopeChip}>{scopeLabel}</span>}
          </div>
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
                <>
                  {accounts.map((a) => (
                    <tr key={a.accountCode}>
                      <td>{a.accountCode}</td>
                      <td>{cleanName(a.accountName)}</td>
                      <td className={styles.num}>{fmt(a.debit, true)}</td>
                      <td className={styles.num}>{fmt(a.credit, true)}</td>
                      <td
                        className={`${styles.num} ${
                          a.balance < 0 ? styles.negative : ""
                        }`}
                      >
                        {balanceCell(a.balance)}
                      </td>
                    </tr>
                  ))}
                  <tr className={styles.totalRow}>
                    <td colSpan={2}>Total</td>
                    <td className={styles.num}>{fmt(totalDebit, true)}</td>
                    <td className={styles.num}>{fmt(totalCredit, true)}</td>
                    <td className={`${styles.num} ${totalBalance === 0 ? styles.positive : styles.negative}`}>
                      {balanceCell(totalBalance)}
                    </td>
                  </tr>
                </>
              )}
            </tbody>
          </table>
          </div>
          <div className={styles.tableNote}>
            Balance = Debit − Credit · Total nets to zero when accounts shown are balanced · rolling 3-month roll-up
          </div>
        </div>
      </div>

      {/* Column 3: stat cards */}
      <div className={styles.statsPane}>
        {scopeLabel && (
          <div className={styles.statsScope}>Period: {scopeLabel}</div>
        )}
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
            status={summary ? (summary.outOfBalance === 0 ? "good" : "bad") : undefined}
          />
          <StatCard
            label="Reconciliation"
            value={recon ? recon.status : ph}
            status={recon ? (recon.status === "BALANCED" ? "good" : "bad") : undefined}
          />
        </div>
      </div>
    </div>
  );
}
