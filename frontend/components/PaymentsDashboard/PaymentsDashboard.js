"use client";

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import Icon from "@leafygreen-ui/icon";
import IconButton from "@leafygreen-ui/icon-button";
import { SegmentedControl, SegmentedControlOption } from "@leafygreen-ui/segmented-control";

import { useDashboard } from "@/lib/api/hooks";
import StatusPill from "@/components/PaymentsWorkflow/StatusPill";
import { BarList, Donut, Legend } from "./Charts";
import styles from "./PaymentsDashboard.module.css";

const VolumeChart = dynamic(() => import("./AxisCharts").then((m) => m.VolumeChart), {
  ssr: false,
  loading: () => <div className={styles.chartSkeleton} />,
});
const TrendChart = dynamic(() => import("./AxisCharts").then((m) => m.TrendChart), {
  ssr: false,
  loading: () => <div className={styles.chartSkeleton} />,
});

const WINDOWS = [
  ["7d", "7 days"],
  ["30d", "30 days"],
];

const TYPE_STYLE = {
  WIRE: { label: "Wire", color: "#00a35c" },
  INTERNAL: { label: "Internal transfer", color: "#889397" },
  ACH: { label: "ACH", color: "#016bf8" },
  CARD: { label: "Card", color: "#9b6ff0" },
};
const typeLabel = (t) => TYPE_STYLE[t]?.label ?? t;
const typeColor = (t) => TYPE_STYLE[t]?.color ?? "#b8c4c2";

const STAGE_COLOR = {
  Initiated: "#c1c7c6",
  Validated: "#89c9f0",
  Enriched: "#016bf8",
  Authorised: "#7fd6a8",
  "On hold": "#f2a33a",
  "In progress": "#e8c547",
  Posted: "#00684a",
  Settled: "#00a35c",
  Reconciled: "#9b6ff0",
  Exception: "#cf4a4a",
  Other: "#b8c4c2",
};

const humanize = (s) => (s ?? "").toLowerCase().replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

function formatSeconds(v) {
  if (v < 1) return `${Math.round(v * 1000)}ms`;
  if (v < 120) return `${v}s`;
  return v < 7200 ? `${Math.round(v / 60)}m` : `${(v / 3600).toFixed(1)}h`;
}

function age(iso) {
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 60) return `${mins}m`;
  if (mins < 1440) return `${Math.round(mins / 60)}h`;
  return `${Math.round(mins / 1440)}d`;
}

function money(amount, currency) {
  if (amount == null) return "—";
  return Number(amount).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) +
    (currency ? ` ${currency}` : "");
}

function Delta({ now, before, lowerIsBetter = false }) {
  if (!before) return <span className={styles.deltaNone}>no prior data</span>;
  const pct = Math.round(((now - before) / before) * 100);
  if (pct === 0) return <span className={styles.deltaNone}>no change</span>;
  const good = lowerIsBetter ? pct < 0 : pct > 0;
  return (
    <span className={good ? styles.deltaGood : styles.deltaBad}>
      {pct > 0 ? "▲" : "▼"} {Math.abs(pct)}% <span className={styles.deltaNote}>vs previous period</span>
    </span>
  );
}

function Panel({ title, action, children, className = "" }) {
  return (
    <section className={`${styles.panel} ${className}`}>
      <header className={styles.panelHeader}>
        <h2>{title}</h2>
        {action}
      </header>
      {children}
    </section>
  );
}

function Empty({ children }) {
  return <p className={styles.empty}>{children}</p>;
}

function Kpi({ label, value, tone, sub, children }) {
  return (
    <div className={`${styles.kpi} ${styles[tone]}`}>
      <div className={styles.kpiLabel}>{label}</div>
      <div className={styles.kpiValue}>{value.toLocaleString()}</div>
      {sub && <div className={styles.kpiSub}>{sub}</div>}
      {children}
    </div>
  );
}

function SystemStatus() {
  const [status, setStatus] = useState(null);
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetch("/api/system-status", { cache: "no-store" })
        .then((r) => r.json())
        .then((d) => !cancelled && setStatus(d))
        .catch(() => !cancelled && setStatus({ operational: false, services: [] }));
    load();
    const id = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);
  if (!status) return null;
  const down = status.services.filter((s) => !s.ok).map((s) => s.name);
  return (
    <span className={status.operational ? styles.sysOk : styles.sysDown} title={down.length ? `Unreachable: ${down.join(", ")}` : undefined}>
      <span className={styles.dot} style={{ background: status.operational ? "#00a35c" : "#cf4a4a" }} />
      {status.operational ? "All systems operational" : `Degraded: ${down.join(", ") || "unknown"}`}
    </span>
  );
}

export default function PaymentsDashboard() {
  const [windowName, setWindowName] = useState("30d");
  const { data, error, loading, updatedAt, refresh } = useDashboard(windowName);

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1>Payments Operations</h1>
          <p>Real-time visibility across the end-to-end payment lifecycle.</p>
        </div>
        <div className={styles.headerTools}>
          <SegmentedControl
            aria-label="Time window"
            size="small"
            value={windowName}
            onChange={setWindowName}
          >
            {WINDOWS.map(([value, label]) => (
              <SegmentedControlOption key={value} value={value}>{label}</SegmentedControlOption>
            ))}
          </SegmentedControl>
          <SystemStatus />
          <span className={styles.updated}>
            {updatedAt ? `Updated ${updatedAt.toLocaleTimeString()}` : "Loading…"}
          </span>
          <IconButton aria-label="Refresh" onClick={refresh}>
            <Icon glyph="Refresh" />
          </IconButton>
        </div>
      </header>

      {error && !data && <div className={styles.error}>Could not load the dashboard: {String(error)}</div>}
      {!data && !error && <div className={styles.chartSkeleton} style={{ height: 320 }} />}
      {data && <Body data={data} loading={loading} />}
    </div>
  );
}

function Body({ data, loading }) {
  const {
    kpis, volume, stages, types, exceptionTrend, exceptionReasons, attention, recent, window,
    processingTimes, reconciliation, settlement, agentImpact,
  } = data;
  const prev = kpis.previous;
  const stageSegments = stages.map((s) => ({ label: s.stage, value: s.count, color: STAGE_COLOR[s.stage] }));
  const typeRows = types.map((t) => ({ label: typeLabel(t.type), value: t.count, color: typeColor(t.type) }));
  const colors = Object.fromEntries(volume.types.map((t) => [t, typeColor(t)]));
  const labels = Object.fromEntries(volume.types.map((t) => [t, typeLabel(t)]));
  const noPayments = kpis.total === 0;

  return (
    <div className={loading ? styles.refreshing : undefined}>
      <div className={styles.kpis}>
        <Kpi label="Total payments" value={kpis.total} tone="kpiGreen">
          <Delta now={kpis.total} before={prev.total} />
        </Kpi>
        <Kpi
          label="Completed"
          value={kpis.completed}
          tone="kpiBlue"
          sub={kpis.successRate != null ? `${kpis.successRate}% of total` : null}
        >
          <Delta now={kpis.completed} before={prev.completed} />
        </Kpi>
        <Kpi label="In progress" value={kpis.inProgress} tone="kpiAmber">
          <Delta now={kpis.inProgress} before={prev.inProgress} lowerIsBetter />
        </Kpi>
        <Kpi label="Exceptions" value={kpis.exceptions} tone="kpiRed">
          <Delta now={kpis.exceptions} before={prev.exceptions} lowerIsBetter />
        </Kpi>
        <Kpi
          label="Resolved by agents"
          value={kpis.resolvedByAgents}
          tone="kpiPurple"
          sub={agentImpact.involved ? `of ${agentImpact.involved} investigated` : "no investigations"}
        />
      </div>

      <div className={styles.row3}>
        <Panel title="Payment volume" className={styles.span2}>
          {noPayments ? (
            <Empty>No payments in this window.</Empty>
          ) : (
            <>
              <VolumeChart
                series={volume.series}
                types={volume.types}
                colors={colors}
                labels={labels}
                bucket={window.bucket}
              />
              <ul className={styles.inlineLegend}>
                {volume.types.map((t) => (
                  <li key={t}><span className={styles.dot} style={{ background: typeColor(t) }} />{typeLabel(t)}</li>
                ))}
              </ul>
            </>
          )}
        </Panel>
        <Panel title="Payment types">
          {typeRows.length ? <BarList rows={typeRows} showShare /> : <Empty>No payments in this window.</Empty>}
        </Panel>
      </div>

      <div className={styles.row2}>
        <Panel title="Payments by lifecycle stage">
          {noPayments ? (
            <Empty>No payments in this window.</Empty>
          ) : (
            <div className={styles.donutRow}>
              <Donut segments={stageSegments} total={kpis.total.toLocaleString()} />
              <Legend segments={stageSegments.filter((s) => s.value > 0)} />
            </div>
          )}
        </Panel>
        <Panel title="Exception trend">
          <TrendChart series={exceptionTrend} bucket={window.bucket} />
        </Panel>
      </div>

      <div className={styles.row2}>
        <Panel title="Top exception reasons">
          {exceptionReasons.length ? (
            <BarList
              rows={exceptionReasons.map((r) => ({ label: humanize(r.reason), value: r.count }))}
              color="#ef8b8b"
            />
          ) : (
            <Empty>No exceptions in this window.</Empty>
          )}
        </Panel>
        <Panel
          title="Exceptions requiring attention"
          action={<Link href="/payments-workflow" className={styles.link}>View all</Link>}
        >
          {attention.length ? (
            <table className={styles.table}>
              <thead>
                <tr><th>Payment ID</th><th>Type</th><th>Reason</th><th>Age</th><th>Severity</th></tr>
              </thead>
              <tbody>
                {attention.map((row) => (
                  <tr key={row.paymentId}>
                    <td><PaymentLink id={row.paymentId} /></td>
                    <td>{typeLabel(row.rail)}</td>
                    <td>{humanize(row.exception?.category)}</td>
                    <td>{row.exception?.createdAt ? age(row.exception.createdAt) : "—"}</td>
                    <td>
                      <StatusPill family={row.exception?.severity === "ACTION_REQUIRED" ? "red" : "gray"}>
                        {humanize(row.exception?.severity)}
                      </StatusPill>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No open exceptions.</Empty>
          )}
        </Panel>
      </div>

      <div className={styles.row2}>
        <Panel title="Average time in stage">
          {processingTimes.length ? (
            <BarList
              rows={processingTimes.map((p) => ({ label: p.stage, value: p.avgSeconds }))}
              format={formatSeconds}
            />
          ) : (
            <Empty>No stage timings in this window.</Empty>
          )}
        </Panel>
        <Panel title="Reconciliation status">
          {reconciliation.total ? (
            <div className={styles.donutRow}>
              <Donut
                segments={[
                  { label: "Reconciled", value: reconciliation.reconciled, color: "#00a35c" },
                  { label: "Pending", value: reconciliation.pending, color: "#e8c547" },
                  { label: "Discrepancies", value: reconciliation.discrepancies, color: "#cf4a4a" },
                ]}
                total={`${reconciliation.reconciledPct}%`}
                caption="Reconciled"
              />
              <Legend
                segments={[
                  { label: "Reconciled", value: reconciliation.reconciled, color: "#00a35c" },
                  { label: "Pending", value: reconciliation.pending, color: "#e8c547" },
                  { label: "Discrepancies", value: reconciliation.discrepancies, color: "#cf4a4a" },
                ]}
              />
            </div>
          ) : (
            <Empty>No payments have reached reconciliation in this window.</Empty>
          )}
        </Panel>
      </div>

      <div className={styles.row2}>
        <Panel title="Settlement activity">
          {settlement.length ? (
            <table className={styles.table}>
              <thead>
                <tr><th>Settlement model</th><th className={styles.num}>Positions</th><th className={styles.num}>Settled</th><th>Status</th></tr>
              </thead>
              <tbody>
                {settlement.map((s) => (
                  <tr key={s.scheme}>
                    <td>{s.scheme}</td>
                    <td className={styles.num}>{s.positions}</td>
                    <td className={styles.num}>{s.settled}</td>
                    <td>
                      {s.returned ? (
                        <StatusPill family="red">{s.returned} returned</StatusPill>
                      ) : s.delayed ? (
                        <StatusPill family="yellow">{s.delayed} delayed</StatusPill>
                      ) : (
                        <StatusPill family="green">On track</StatusPill>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No settlement positions in this window.</Empty>
          )}
        </Panel>
        <Panel title="Agent impact">
          <div className={styles.agentRate}>
            <span>Agent success rate</span>
            <strong>{agentImpact.successRate != null ? `${agentImpact.successRate}%` : "—"}</strong>
          </div>
          <div className={styles.barTrack}>
            <span className={styles.barFill} style={{ width: `${agentImpact.successRate ?? 0}%`, background: "#00a35c" }} />
          </div>
          <p className={styles.agentNote}>
            {agentImpact.involved
              ? `${agentImpact.resolved} of ${agentImpact.involved} reconciliation issues resolved autonomously`
              : "The reconciliation agent has not investigated any issues in this window."}
          </p>
          {agentImpact.recent.map((a) => (
            <div key={a.exceptionId} className={styles.insight}>
              <div className={styles.insightHead}>
                <PaymentLink id={a.paymentId} />
                <StatusPill family={a.verification === "RESOLVED" ? "green" : "yellow"}>
                  {humanize(a.verification ?? a.status)}
                </StatusPill>
              </div>
              <div>{a.rootCause ?? humanize(a.category)}</div>
              {a.confidence && <div className={styles.deltaNone}>Confidence: {humanize(a.confidence)}</div>}
            </div>
          ))}
        </Panel>
      </div>

      <Panel title="Recent payments" action={<Link href="/payments-workflow" className={styles.link}>View all</Link>}>
        {recent.length ? (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Payment ID</th><th>Type</th><th>Amount</th><th>Counterparty</th><th>Status</th><th>Last update</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((p) => (
                <tr key={p.paymentId}>
                  <td><PaymentLink id={p.paymentId} /></td>
                  <td>{typeLabel(p.rail)}{p.direction === "INBOUND" ? " (in)" : ""}</td>
                  <td className={styles.num}>{money(p.amount, p.currency)}</td>
                  <td>{p.direction === "INBOUND" ? p.debtor?.name : p.creditor?.name ?? "—"}</td>
                  <td><StatusPill status={p.status} /></td>
                  <td>{new Date(p.lifecycle?.stateEnteredAt ?? p.createdAt).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No payments yet.</Empty>
        )}
      </Panel>
    </div>
  );
}

function PaymentLink({ id }) {
  return (
    <Link href={`/payments-workflow?payment=${encodeURIComponent(id)}`} className={styles.link}>
      {id}
    </Link>
  );
}
