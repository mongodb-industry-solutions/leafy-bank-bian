"use client";

// GL Pipeline Monitor — ported from the static reference at
// .docs/gl-monitor-static-ref. Orchestration mirrors app.js: fetch health
// first (establishes the batch window), then fan out to the four columns.
// Reads flow through the /api/backend/pipeline proxy (pipelineApi).
import { pipelineApi } from "@/lib/api/client";
import { filterByBatch } from "@/lib/glMonitor/batch";
import { buildParams } from "@/lib/glMonitor/format";
import {
  SegmentedControl,
  SegmentedControlOption,
} from "@leafygreen-ui/segmented-control";
import { useCallback, useEffect, useRef, useState } from "react";
import DetailPanel from "./DetailPanel";
import styles from "./GlMonitor.module.css";
import HealthBar from "./HealthBar";
import InitiatePanel from "./InitiatePanel";
import PaymentTrace from "./PaymentTrace";
import PipelineColumns from "./PipelineColumns";
import PipelineStepper from "./PipelineStepper";

const EMPTY_COL = { items: [], count: "—", loading: false, error: null };
const DETAIL_BG = {
  tx: "var(--col1)",
  le: "var(--col2)",
  sl: "var(--col3)",
  jn: "var(--col4)",
};
const DETAIL_TITLE = {
  tx: (d) => `Transaction: ${d.paymentId}`,
  le: (d) => `Ledger Event: ${d.eventId}`,
  sl: (d) => `SubLedger Entry: ${d.subLedgerId}`,
  jn: (d) => `Journal Entry: ${d.journalId}`,
};

export default function GlMonitor() {
  const [period, setPeriod] = useState("");
  const [status, setStatus] = useState("");
  const [health, setHealth] = useState(null);
  const [healthStatus, setHealthStatus] = useState("checking…");
  const [columns, setColumns] = useState({
    tx: EMPTY_COL,
    le: EMPTY_COL,
    sl: EMPTY_COL,
    jn: EMPTY_COL,
  });
  const [selected, setSelected] = useState({ col: null, idx: null });
  const [detail, setDetail] = useState({ open: false });
  const [tab, setTab] = useState(0);
  const [tracePid, setTracePid] = useState("");

  // Latest filter values, read inside async fetchers to avoid stale closures.
  const filtersRef = useRef({ period, status });
  filtersRef.current = { period, status };

  const setCol = (key, patch) =>
    setColumns((c) => ({ ...c, [key]: { ...c[key], ...patch } }));

  // Returns the batch info so callers can pass it straight into the column
  // fetchers without waiting on a state update.
  const fetchHealth = useCallback(async () => {
    const { data, error } = await pipelineApi("health");
    if (error) {
      setHealthStatus(`error: ${error}`);
      return null;
    }
    setHealth(data);
    setHealthStatus(`updated ${new Date().toLocaleTimeString()}`);
    return {
      lastBatchAt: data.lastBatchAt || null,
      intervalMs: (data.batchIntervalSeconds || 600) * 1000,
    };
  }, []);

  const fetchColumn = useCallback(
    async (key, buildPath, tsField, batchInfo) => {
      setCol(key, { loading: true, error: null });
      const { data, error } = await pipelineApi(buildPath());
      if (error) {
        setCol(key, { loading: false, error, items: [] });
        return;
      }
      const items = filterByBatch(data.items || [], tsField, batchInfo);
      const total = data.total ?? data.items?.length ?? 0;
      const batchMode = batchInfo?.lastBatchAt != null;
      setCol(key, {
        loading: false,
        error: null,
        items,
        count: batchMode ? `${items.length} / ${total}` : `${total}`,
      });
    },
    [],
  );

  const refreshAll = useCallback(async () => {
    const batchInfo = await fetchHealth();
    const { period: p, status: s } = filtersRef.current;
    fetchColumn(
      "tx",
      () => `transactions${buildParams(p, { limit: 100 })}`,
      "createdAt",
      batchInfo,
    );
    fetchColumn(
      "le",
      () =>
        `ledger-events${buildParams(p, s ? { status: s, limit: 100 } : { limit: 100 })}`,
      "occurredAt",
      batchInfo,
    );
    fetchColumn(
      "sl",
      () => `subledger-entries${buildParams(p, { limit: 100 })}`,
      "postingDate",
      batchInfo,
    );
    fetchColumn(
      "jn",
      () => `journals${buildParams(p, { limit: 100 })}`,
      "createdAt",
      batchInfo,
    );
  }, [fetchHealth, fetchColumn]);

  // Mount: initial load + 10s health poll (app.js parity).
  useEffect(() => {
    refreshAll();
    const id = setInterval(fetchHealth, 10_000);
    return () => clearInterval(id);
  }, [refreshAll, fetchHealth]);

  const onSelect = (col, idx, item) => {
    setSelected({ col, idx });
    setDetail({
      open: true,
      kind: col,
      data: item,
      bg: DETAIL_BG[col],
      title: DETAIL_TITLE[col](item),
    });
  };

  const closeDetail = () => {
    setDetail({ open: false });
    setSelected({ col: null, idx: null });
  };

  // From a Transactions card's chip: jump to the Payment Trace tab and trace
  // that payment end-to-end (replaces the old highlight-the-LE-card behavior).
  const onTraceTx = (paymentId) => {
    if (!paymentId) return;
    setTracePid(paymentId);
    setTab(1);
  };

  return (
    <div className={styles.glMonitorRoot}>
      <HealthBar health={health} statusText={healthStatus} />
      <div className={styles["gl-tabs"]}>
        <div className={styles["gl-mode-switch"]}>
          <SegmentedControl
            name="gl-mode"
            value={String(tab)}
            onChange={(v) => setTab(Number(v))}
          >
            <SegmentedControlOption value="0">
              Pipeline Monitor (for Multiple Transactions)
            </SegmentedControlOption>
            <SegmentedControlOption value="1">
              Payment Trace (for Single Transactions)
            </SegmentedControlOption>
          </SegmentedControl>
        </div>
        {tab === 0 ? (
          <>
            <InitiatePanel onFired={refreshAll} onRefresh={refreshAll} />
            <PipelineStepper />
            <PipelineColumns
              columns={columns}
              selected={selected}
              onSelect={onSelect}
              onTraceTx={onTraceTx}
            />
            <DetailPanel detail={detail} onClose={closeDetail} />
          </>
        ) : (
          <PaymentTrace initialPid={tracePid} />
        )}
      </div>
    </div>
  );
}
