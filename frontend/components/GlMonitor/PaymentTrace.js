"use client";

// Payment Trace (End-to-End) — Doina's back-office design (see
// .docs/frontend/back-office.png, screenshots 2 & 3). Rough layout for visual
// review: search a paymentId, then show a horizontal 6-stage rail with
// completion/status + a row of per-stage detail cards with "View JSON".
// Wired to GET /pipeline/trace/{paymentId} (returns 4 stages; the 6 UI stages
// are derived from those — see buildStages).
import React, { useState, useEffect, useCallback } from "react";
import Icon from "@leafygreen-ui/icon";
import Code from "@leafygreen-ui/code";
import styles from "./GlMonitor.module.css";
import { Chip } from "./Bits";
import { fmt, fmtMinor, fmtDate } from "@/lib/glMonitor/format";
import { pipelineApi } from "@/lib/api/client";

// Maps the 4 trace docs → 6 presentation stages Doina drew. Rough mapping;
// "Payment" vs "Transaction" both draw from the transaction doc for now, and
// "Batch Queue" is derived from ledger-event posting status (open question in
// the carry-forward doc — refine later).
function buildStages(d) {
  const tx = d.transaction;
  const le = d.ledgerEvent;
  const sls = d.subLedgerEntries || [];
  const jn = d.journalEntry;

  const batchStatus = !le ? null : le.postingStatus === "POSTED" ? "POSTED" : "QUEUED";

  return [
    {
      key: "payment", hue: "green", title: "Payment", icon: "CreditCard",
      id: tx?.paymentId, count: null,
      status: tx ? "COMPLETED" : null, reached: !!tx,
      pairs: tx ? [
        ["Type", tx.paymentType || tx.rail || "—"],
        ["Amount", `$${fmt(tx.amount)} ${tx.currency || "USD"}`],
        ["Status", <Chip status={tx.transactionStatus} key="c" />],
        ["Initiated", fmtDate(tx.createdAt)],
      ] : null, raw: tx,
    },
    {
      key: "transaction", hue: "green", title: "Transaction", icon: "Beaker",
      id: tx?.paymentId, count: null,
      status: tx ? tx.transactionStatus : null, reached: !!tx,
      pairs: tx ? [
        ["From", tx.payer?.accountId],
        ["To", tx.payee?.accountId],
        ["Rail", tx.rail || "—"],
        ["Amount", `$${fmt(tx.amount)} ${tx.currency || "USD"}`],
      ] : null, raw: tx,
    },
    {
      key: "ledgerEvents", hue: "blue", title: "Ledger Events", icon: "Diagram3",
      id: le?.eventId, count: le ? 1 : 0,
      status: le?.postingStatus, reached: !!le,
      pairs: le ? [
        ["Event", le.eventType || "—"],
        ["DR", le.debitLeg?.glAccountCode || "—"],
        ["CR", le.creditLeg?.glAccountCode || "—"],
        ["Amount", fmtMinor(le.debitLeg?.amount)],
      ] : null, raw: le,
    },
    {
      key: "subledger", hue: "blue", title: "Subledger", icon: "List",
      id: sls[0]?.subLedgerId, count: sls.length,
      status: sls.length ? (sls.every((s) => s.journalEntryId) ? "POSTED" : "PENDING") : null,
      reached: sls.length > 0,
      pairs: sls.length ? sls.slice(0, 3).map((s) => [
        s.side, `${s.controlAccountCode} · ${fmtMinor(s.amount)}`,
      ]) : null, raw: sls,
    },
    {
      key: "batch", hue: "purple", title: "Batch Queue", icon: "Clock",
      id: jn?.batchId || le?.postingResult?.journalEntryId, count: null,
      status: batchStatus, reached: !!le,
      pairs: le ? [
        ["Queue", batchStatus === "POSTED" ? "Cleared" : "Awaiting batch"],
        ["Journal", le.postingResult?.journalEntryId || "—"],
      ] : null, raw: le?.postingResult,
    },
    {
      key: "generalLedger", hue: "purple", title: "General Ledger", icon: "Building",
      id: jn?.journalId, count: jn ? (jn.entries || []).length : 0,
      status: jn?.status, reached: !!jn,
      pairs: jn ? [
        ["Batch", jn.batchId || "—"],
        ["Lines", (jn.entries || []).length],
        ["Total", fmtMinor(jn.totalAmount)],
        ["Posted", fmtDate(jn.createdAt)],
      ] : null, raw: jn,
    },
  ];
}

// Vertical stepper (left rail). Clicking a reached step selects it.
function StepList({ stages, selectedKey, onSelect }) {
  return (
    <div className={styles["ptrace-steplist"]}>
      {stages.map((s) => (
        <button
          key={s.key}
          type="button"
          className={`${styles["ptrace-stepitem"]} ${styles[`ptrace-${s.hue}`]} ${selectedKey === s.key ? styles["ptrace-stepitem-active"] : ""}`}
          disabled={!s.reached}
          onClick={() => onSelect(s.key)}
        >
          <div className={`${styles["ptrace-circle"]} ${s.reached ? "" : styles["ptrace-dim"]}`}>
            <Icon glyph={s.icon} size={18} />
          </div>
          <div className={styles["ptrace-stepitem-text"]}>
            <div className={styles["ptrace-step-title"]}>{s.title}</div>
            <div className={styles["ptrace-step-id"]}>{s.id || (s.count ? `${s.count} events` : "—")}</div>
          </div>
          <div className={styles["ptrace-stepitem-mark"]}>
            {s.reached
              ? <Icon glyph="CheckmarkWithCircle" size={18} fill="#16a34a" />
              : <span className={styles["ptrace-step-dot"]} />}
          </div>
        </button>
      ))}
    </div>
  );
}

// Right-hand detail pane for the selected step.
function DetailPane({ stage }) {
  const [showJson, setShowJson] = useState(false);
  if (!stage) return null;
  return (
    <div className={`${styles["ptrace-detail"]} ${styles[`ptrace-${stage.hue}`]}`}>
      <div className={styles["ptrace-detail-title"]}>
        {stage.title} Details{stage.count != null && ` (${stage.count})`}
      </div>
      {stage.reached ? (
        <>
          <table className={styles["kv-table"]}>
            <tbody>
              {(stage.pairs || []).map(([k, v], i) => (
                <tr key={i}><td>{k}</td><td>{v ?? "—"}</td></tr>
              ))}
            </tbody>
          </table>
          <button className={styles["ptrace-json-btn"]} onClick={() => setShowJson((s) => !s)}>
            <Icon glyph="CurlyBraces" size={12} /> {showJson ? "Hide JSON" : "View JSON"}
          </button>
          {showJson && <div style={{ marginTop: 8 }}><Code language="json">{JSON.stringify(stage.raw, null, 2)}</Code></div>}
        </>
      ) : (
        <div className={styles["ptrace-not-reached"]}>not reached</div>
      )}
    </div>
  );
}

export default function PaymentTrace() {
  const [pid, setPid] = useState("");
  const [state, setState] = useState({ kind: "empty" }); // empty|loading|notfound|error|data
  const [recent, setRecent] = useState([]); // last 5 payments to pick from
  const [selectedKey, setSelectedKey] = useState(null); // selected stage in the vertical stepper

  const fetchTrace = useCallback(async (idArg) => {
    const id = (idArg ?? pid).trim();
    if (!id) return;
    setPid(id);
    setState({ kind: "loading" });
    const { data, error } = await pipelineApi(`trace/${encodeURIComponent(id)}`);
    if (error) {
      setState(error.includes("404") ? { kind: "notfound", id } : { kind: "error", error });
      return;
    }
    setState({ kind: "data", d: data });
  }, [pid]);

  // Load the 5 most recent payments so the user can trace one without typing.
  useEffect(() => {
    let alive = true;
    pipelineApi("transactions?limit=5").then(({ data }) => {
      if (alive && data?.items) setRecent(data.items);
    });
    return () => { alive = false; };
  }, []);

  const stages = state.kind === "data" ? buildStages(state.d) : null;

  // On a new trace, default the selection to the last reached stage.
  useEffect(() => {
    if (!stages) { setSelectedKey(null); return; }
    const reached = stages.filter((s) => s.reached);
    setSelectedKey((reached[reached.length - 1] || stages[0]).key);
  }, [state]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedStage = stages?.find((s) => s.key === selectedKey) || null;

  return (
    <div className={styles["ptrace-root"]}>
      {/* Recent transactions — pick one to trace without typing an id */}
      <div className={styles.panel} style={{ marginBottom: 16 }}>
        <div className={styles["panel-header"]}>
          <span className={styles["panel-title"]}>Recent Transactions</span>
          <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 8 }}>
            last 5 payments · click one to trace it end-to-end
          </span>
        </div>
        <div className={styles["panel-body"]}>
          {recent.length ? (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {recent.map((t) => (
                <button
                  key={t.paymentId}
                  className={styles["preset-btn"]}
                  style={pid === t.paymentId ? { borderColor: "var(--accent)", background: "var(--accent-light)" } : undefined}
                  onClick={() => fetchTrace(t.paymentId)}
                  title={`${t.payer?.accountId || "—"} → ${t.payee?.accountId || "—"} · ${fmtDate(t.createdAt)}`}
                >
                  <span style={{ fontFamily: "monospace", fontWeight: 700 }}>{t.paymentId}</span>
                  {" · "}${fmt(t.amount)} {t.currency || "USD"}
                  {" · "}<Chip status={t.transactionStatus} />
                </button>
              ))}
            </div>
          ) : (
            <div className={styles["empty-state"]}>No recent transactions.</div>
          )}
        </div>
      </div>

      {state.kind === "empty" && <div className={styles["empty-state"]}>Enter a paymentId to trace it end-to-end across all stages.</div>}
      {state.kind === "loading" && <div className={styles["empty-state"]}>Tracing…</div>}
      {state.kind === "notfound" && <div className={styles["empty-state"]}>Payment not found: {state.id}</div>}
      {state.kind === "error" && <div className={styles["error-state"]}>Error: {state.error}</div>}

      {stages && (
        <div className={styles["ptrace-explorer"]}>
          <div className={styles["panel-header"]} style={{ gridColumn: "1 / -1", marginBottom: 8 }}>
            <span className={styles["panel-title"]}>Payment Trace</span>
            <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 8 }}>Click on one of the steps to view more details</span>
          </div>
          {/* Vertical stage rail (left) + detail pane (right) */}
          <div style={{ gridColumn: "1 / -1", display: "grid", gridTemplateColumns: "300px 1fr", gap: 20, paddingLeft: 12, paddingRight: 12 }}>
            <StepList stages={stages} selectedKey={selectedKey} onSelect={setSelectedKey} />
            <DetailPane stage={selectedStage} />
          </div>
        </div>
      )}
    </div>
  );
}
