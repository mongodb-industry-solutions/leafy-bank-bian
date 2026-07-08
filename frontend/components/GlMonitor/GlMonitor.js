"use client";

// GL Pipeline Monitor — ported from the static reference at
// .docs/gl-monitor-static-ref. Orchestration mirrors app.js: fetch health
// first (establishes the batch window), then fan out to the four columns.
// Reads flow through the /api/backend/pipeline proxy (pipelineApi).
import { pipelineApi } from "@/lib/api/client";
import { filterByBatch } from "@/lib/glMonitor/batch";
import { buildParams } from "@/lib/glMonitor/format";
import { computeLineage } from "@/lib/glMonitor/lineage";
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

export default function GlMonitor({ lastBatchAt, onManualRefresh }) {
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
  const [lineage, setLineage] = useState(null);
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
    async (key, buildPath, tsField, batchInfoPromise) => {
      setCol(key, { loading: true, error: null });
      // Fire the column request and the health request concurrently; only the
      // client-side batch filter needs the health result, so await it here.
      const [{ data, error }, batchInfo] = await Promise.all([
        pipelineApi(buildPath()),
        batchInfoPromise,
      ]);
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
    // Fire health and all four columns in parallel; columns await the shared
    // health promise internally only for the batch-window filter.
    const batchInfoPromise = fetchHealth();
    const { period: p, status: s } = filtersRef.current;
    fetchColumn(
      "tx",
      () => `transactions${buildParams(p, { limit: 100 })}`,
      "createdAt",
      batchInfoPromise,
    );
    fetchColumn(
      "le",
      () =>
        `ledger-events${buildParams(p, s ? { status: s, limit: 100 } : { limit: 100 })}`,
      "occurredAt",
      batchInfoPromise,
    );
    fetchColumn(
      "sl",
      () => `subledger-entries${buildParams(p, { limit: 100 })}`,
      "postingDate",
      batchInfoPromise,
    );
    fetchColumn(
      "jn",
      () => `journals${buildParams(p, { limit: 100 })}`,
      "createdAt",
      batchInfoPromise,
    );
  }, [fetchHealth, fetchColumn]);

  // Mount: initial load. Health is fetched once as part of refreshAll (it
  // establishes the batch window); no standing poll — nothing renders live
  // health, and the batch cadence is 10 min, not 10 s.
  useEffect(() => {
    refreshAll();
  }, [refreshAll]);

  // The journal column only changes when the GL batch posts, which can be
  // minutes after a payment fires. GlPipelineView polls for the batch actually
  // completing (lastBatchAt) — re-fetch all columns when it advances, so the
  // journal stage fills in on its own without a manual refresh. (The dashboard
  // reacts to the same lastBatchAt via its refreshKey, up in GlPipelineView.)
  const prevBatchAt = useRef(null);
  useEffect(() => {
    if (lastBatchAt == null) return;
    // Record the first observed value without refreshing (it's already loaded).
    if (prevBatchAt.current == null) {
      prevBatchAt.current = lastBatchAt;
      return;
    }
    if (lastBatchAt !== prevBatchAt.current) {
      prevBatchAt.current = lastBatchAt;
      refreshAll();
    }
  }, [lastBatchAt, refreshAll]);

  // Manual "Refresh All": refresh the columns and also nudge the sibling
  // dashboard (via GlPipelineView) so the whole page updates together.
  const handleManualRefresh = useCallback(() => {
    refreshAll();
    onManualRefresh?.();
  }, [refreshAll, onManualRefresh]);

  const onSelect = (col, idx, item) => {
    // Clicking the already-selected card toggles the selection off.
    if (selected.col === col && selected.idx === idx) {
      closeDetail();
      return;
    }
    setSelected({ col, idx });
    setLineage(computeLineage(col, item, columns));
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
    setLineage(null);
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
      {/* <HealthBar health={health} statusText={healthStatus} /> */}
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
            <InitiatePanel
              onFired={refreshAll}
              onRefresh={handleManualRefresh}
              nextBatchAt={health?.nextBatchAt ?? null}
              batchIntervalSeconds={health?.batchIntervalSeconds ?? 600}
              onBatchTriggered={handleManualRefresh}
            />
            <PipelineStepper />
            <PipelineColumns
              columns={columns}
              selected={selected}
              lineage={lineage}
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
