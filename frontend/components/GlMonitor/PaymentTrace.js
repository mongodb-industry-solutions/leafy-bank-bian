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

// Maps the trace docs → presentation stages Doina drew. "Batch Queue" is
// derived from ledger-event posting status, and only applies to BATCH-mode
// rails — REALTIME/NEAR_REALTIME rails (e.g. INTERNAL, WIRE) post straight to
// the general ledger with no queueing step.
function buildStages(d) {
  const pay = d.payment;
  const tx = d.transaction;
  const le = d.ledgerEvent;
  const sls = d.subLedgerEntries || [];
  const jn = d.journalEntry;
  const isRealtime = ["REALTIME", "NEAR_REALTIME"].includes(d.postingMode);

  const batchStatus = !le ? null : le.postingStatus === "POSTED" ? "POSTED" : "QUEUED";

  const stages = [
    {
      key: "payment", hue: "green", title: "Payment", icon: "CreditCard",
      id: pay?.paymentId || tx?.paymentId, count: null,
      status: pay?.status, reached: !!pay,
      pairs: pay ? [
        ["Txn ID", pay.txnId || "—"],
        ["End-to-End ID", pay.endToEndId || "—"],
        ["Status", <Chip status={pay.status} key="c" />],
        ["Type / Rail", `${pay.type || "—"} · ${pay.rail || "—"}`],
        ["Priority", pay.priority || "—"],
        ["Amount", `$${fmt(pay.amount)} ${pay.currency || "USD"}`],
        ["Debtor", pay.debtor ? `${pay.debtor.name} (${pay.debtor.accountId})` : "—"],
        ["Creditor", pay.creditor ? `${pay.creditor.name} (${pay.creditor.accountId})` : "—"],
        ["Remittance", pay.remittance?.unstructured || "—"],
        ["Sanctions", pay.correspondent?.sanctionsCheck?.status || "—"],
        ["Fraud", pay.fraud ? `${pay.fraud.decision} (score ${pay.fraud.score})` : "—"],
        ["Channel", pay.initiation?.channel || "—"],
        ["Internal", pay.isInternal ? "Yes" : "No"],
        ["Initiated", fmtDate(pay.initiatedAt)],
        ["Settled", pay.clearing?.settledAt ? fmtDate(pay.clearing.settledAt) : "—"],
      ] : null, raw: pay, features: null,
    },
    {
      key: "transaction", hue: "green", title: "Transaction", icon: "Beaker",
      id: tx?.paymentId, count: null,
      status: tx ? tx.transactionStatus : null, reached: !!tx,
      features: ["ACID multi-doc transaction", "$inc atomic balance update", "DuplicateKeyError idempotency"],
      pairs: tx ? [
        ["Bank Ref", tx.bankRef || "—"],
        ["Status", <Chip status={tx.transactionStatus} key="c" />],
        ["Type / Rail", `${tx.paymentType || "—"} · ${tx.rail || "—"}`],
        ["Direction", tx.direction || "—"],
        ["Txn Code", tx.txnCode || "—"],
        ["Amount", `$${fmt(tx.amount)} ${tx.currency || "USD"}`],
        ["Base Amount", tx.baseAmount != null ? `$${fmt(tx.baseAmount)}` : "—"],
        ["Balance After", tx.balanceAfter != null ? `$${fmt(tx.balanceAfter)}` : "—"],
        ["From", tx.payer ? `${tx.payer.name} (${tx.payer.accountId})` : "—"],
        ["To", tx.payee ? `${tx.payee.name} (${tx.payee.accountId})` : "—"],
        ["Description", tx.description || "—"],
        ["Category", tx.transactionCategory || "—"],
        ["Channel", tx.channel || "—"],
        ["Value Date", tx.valueDate || "—"],
        ["Booking Date", tx.bookingDate || "—"],
        ["Reversed", tx.isReversed ? `Yes (${tx.reversalTxnId || "—"})` : "No"],
        ["Created", fmtDate(tx.createdAt)],
      ] : null, raw: tx,
    },
    {
      key: "ledgerEvents", hue: "blue", title: "Ledger Events", icon: "Diagram3",
      id: le?.eventId, count: le ? 1 : 0,
      status: le?.postingStatus, reached: !!le,
      features: ["Change Streams", "Resume Tokens", "DuplicateKeyError idempotency", "$jsonSchema validator"],
      ledger: le ? {
        currency: le.debitLeg?.currency || le.creditLeg?.currency || "USD",
        debits: le.debitLeg ? [{
          accountCode: le.debitLeg.glAccountCode,
          accountName: le.debitLeg.controlAccountCode ? `ctrl ${le.debitLeg.controlAccountCode}` : "",
          amount: le.debitLeg.amount,
        }] : [],
        credits: le.creditLeg ? [{
          accountCode: le.creditLeg.glAccountCode,
          accountName: le.creditLeg.controlAccountCode ? `ctrl ${le.creditLeg.controlAccountCode}` : "",
          amount: le.creditLeg.amount,
        }] : [],
      } : null,
      pairs: le ? [
        ["Status", <Chip status={le.postingStatus} key="c" />],
        ["Event Type", le.eventType || "—"],
        ["Description", le.description || "—"],
        ["Posting Mode", le.postingMode?.type || "—"],
        ["Rail", le.rail || "—"],
        ["DR Account", `${le.debitLeg?.glAccountCode || "—"} (ctrl ${le.debitLeg?.controlAccountCode || "—"})`],
        ["DR Entity", le.debitLeg?.entityReference?.entityId || "—"],
        ["CR Account", `${le.creditLeg?.glAccountCode || "—"} (ctrl ${le.creditLeg?.controlAccountCode || "—"})`],
        ["CR Entity", le.creditLeg?.entityReference?.entityId || "—"],
        ["Amount", `${fmtMinor(le.debitLeg?.amount)} ${le.debitLeg?.currency || "—"}`],
        ["Occurred", fmtDate(le.occurredAt)],
        ["Value Date", fmtDate(le.valueDate)],
        ["Period", le.periodName || "—"],
        ["Sub Ledger Type", le.meta?.subLedgerType || "—"],
        ["Source", le.sourceReference ? `${le.sourceReference.sourceType} · ${le.sourceReference.sourceCollection}` : "—"],
        ["Journal Entry", le.postingResult?.journalEntryId || "—"],
        ["Posted At", le.postingResult?.postedAt ? fmtDate(le.postingResult.postedAt) : "—"],
        ["Reversal Of", le.reversalOf || "—"],
      ] : null, raw: le,
    },
    {
      key: "subledger", hue: "blue", title: "Subledger", icon: "List",
      id: sls[0]?.subLedgerId, count: sls.length,
      status: sls.length ? (sls.every((s) => s.journalEntryId) ? "POSTED" : "PENDING") : null,
      reached: sls.length > 0,
      features: ["Change Streams", "ACID multi-doc transaction", "$jsonSchema validator", "Partial index (journalEntryId ≠ \"\")"],
      ledger: sls.length ? {
        currency: sls[0]?.currency || "USD",
        debits: sls.filter((s) => s.side === "DEBIT").map((s) => ({
          accountCode: s.controlAccountCode, accountName: s.subLedgerType || "", amount: s.amount,
        })),
        credits: sls.filter((s) => s.side === "CREDIT").map((s) => ({
          accountCode: s.controlAccountCode, accountName: s.subLedgerType || "", amount: s.amount,
        })),
      } : null,
      pairs: sls.length ? (() => {
        const debit = sls.find((s) => s.side === "DEBIT") || sls[0];
        const credit = sls.find((s) => s.side === "CREDIT") || sls[1];
        return [
          ["Journal Entry", debit?.journalEntryId || credit?.journalEntryId || "—"],
          ["Period", debit?.periodCode || credit?.periodCode || "—"],
          ["Value Date", fmtDate(debit?.valueDate || credit?.valueDate)],
          ["Posting Date", fmtDate(debit?.postingDate || credit?.postingDate)],
          ["DR SubLedger ID", debit?.subLedgerId || "—"],
          ["DR Control A/C", debit?.controlAccountCode || "—"],
          ["DR Type", debit?.subLedgerType || "—"],
          ["DR Entity", debit?.entityReference?.entityId || "—"],
          ["DR Amount", debit ? `${fmtMinor(debit.amount)} ${debit.currency || "—"}` : "—"],
          ["DR Running Balance", debit ? fmtMinor(debit.runningBalance) : "—"],
          ["DR Status", debit ? <Chip status={debit.status} key="dr" /> : "—"],
          ["CR SubLedger ID", credit?.subLedgerId || "—"],
          ["CR Control A/C", credit?.controlAccountCode || "—"],
          ["CR Type", credit?.subLedgerType || "—"],
          ["CR Entity", credit?.entityReference?.entityId || "—"],
          ["CR Amount", credit ? `${fmtMinor(credit.amount)} ${credit.currency || "—"}` : "—"],
          ["CR Running Balance", credit ? fmtMinor(credit.runningBalance) : "—"],
          ["CR Status", credit ? <Chip status={credit.status} key="cr" /> : "—"],
        ];
      })() : null, raw: sls,
    },
    ...(isRealtime ? [] : [{
      key: "batch", hue: "purple", title: "Batch Queue", icon: "Clock",
      id: jn?.batchId || le?.postingResult?.journalEntryId, count: null,
      status: batchStatus, reached: !!le,
      features: ["Aggregation ($group $sum $cond) — pre-batch reconciliation gate"],
      pairs: le ? [
        ["Queue", batchStatus === "POSTED" ? "Cleared" : "Awaiting batch"],
        ["Journal", le.postingResult?.journalEntryId || "—"],
      ] : null, raw: le?.postingResult,
    }]),
    {
      key: "generalLedger", hue: "purple", title: "General Ledger", icon: "Building",
      id: jn?.journalId, count: jn ? (jn.entries || []).length : 0,
      status: jn?.status, reached: !!jn,
      features: ["Aggregation ($group $sum $addToSet)", "ACID transaction", "$expr Pacioli validator", "Multikey index (entries.accountCode)"],
      // Double-entry legs rendered as a T-account (see TAccount); kept out of
      // the flat pairs so the balance story reads spatially, not as a list.
      ledger: jn ? {
        currency: jn.currency || "USD",
        debits: (jn.entries || []).filter((e) => e.side === "DEBIT"),
        credits: (jn.entries || []).filter((e) => e.side === "CREDIT"),
      } : null,
      pairs: jn ? [
        ["Status", <Chip status={jn.status} key="c" />],
        ["Journal Type", jn.journalType || "—"],
        ["Source", jn.sourceReference ? `${jn.sourceReference.sourceType} · ${jn.sourceReference.sourceId}` : "—"],
        ["Txn Count", jn.sourceReference?.txnCount ?? "—"],
        ["Period", jn.periodCode || "—"],
        ["Value / Posting Date", `${jn.valueDate || "—"} / ${jn.postingDate || "—"}`],
        ["Created By", jn.createdBy || "—"],
        ["Mapping Version", jn.mappingVersion || "—"],
        ["Created", fmtDate(jn.createdAt)],
      ] : null, raw: jn,
    },
  ];
  return stages;
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

// Double-entry T-account: debits left, credits right, totals footer, and a
// balanced badge. Makes DR == CR visible spatially instead of as flat rows.
function TAccount({ ledger }) {
  const sum = (rows) => rows.reduce((t, e) => t + (Number(e.amount) || 0), 0);
  const drTotal = sum(ledger.debits);
  const crTotal = sum(ledger.credits);
  const balanced = drTotal === crTotal;
  const rows = Math.max(ledger.debits.length, ledger.credits.length);

  const cell = (e) =>
    e ? (
      <>
        <span className={styles["ta-acct"]}>
          {e.accountCode}{e.accountName ? ` · ${e.accountName}` : ""}
        </span>
        <span className={styles["ta-amt"]}>${fmt(e.amount, true)}</span>
      </>
    ) : null;

  return (
    <div className={styles["ta-wrap"]}>
      <div className={styles["ta-head"]}>
        <span>Debit</span>
        <span>Credit</span>
      </div>
      <div className={styles["ta-body"]}>
        {Array.from({ length: rows }).map((_, i) => (
          <div className={styles["ta-row"]} key={i}>
            <div className={styles["ta-cell"]}>{cell(ledger.debits[i])}</div>
            <div className={styles["ta-cell"]}>{cell(ledger.credits[i])}</div>
          </div>
        ))}
      </div>
      <div className={styles["ta-foot"]}>
        <div className={styles["ta-cell"]}>
          <span className={styles["ta-acct"]}>Total DR</span>
          <span className={styles["ta-amt"]}>${fmt(drTotal, true)}</span>
        </div>
        <div className={styles["ta-cell"]}>
          <span className={styles["ta-acct"]}>Total CR</span>
          <span className={styles["ta-amt"]}>${fmt(crTotal, true)}</span>
        </div>
      </div>
      <div className={`${styles["ta-badge"]} ${balanced ? styles["ta-ok"] : styles["ta-bad"]}`}>
        <Icon glyph={balanced ? "CheckmarkWithCircle" : "Warning"} size={14} />
        {balanced
          ? `Balanced — DR $${fmt(drTotal, true)} = CR $${fmt(crTotal, true)} ${ledger.currency}`
          : `Out of balance — DR $${fmt(drTotal, true)} ≠ CR $${fmt(crTotal, true)}`}
      </div>
    </div>
  );
}

// Right-hand detail pane for the selected step.
function DetailPane({ stage }) {
  const [showJson, setShowJson] = useState(false);
  const [copied, setCopied] = useState(false);
  if (!stage) return null;
  const jsonText = stage.raw ? JSON.stringify(stage.raw, null, 2) : "";
  const copyJson = () => {
    navigator.clipboard.writeText(jsonText);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className={`${styles["ptrace-detail"]} ${styles[`ptrace-${stage.hue}`]}`}>
      <div className={styles["ptrace-detail-title"]}>
        {stage.title} Details{stage.count != null && ` (${stage.count})`}
      </div>
      {stage.reached ? (
        <>
          {stage.ledger && <TAccount ledger={stage.ledger} />}
          <table className={styles["kv-table"]}>
            <tbody>
              {(stage.pairs || []).map(([k, v], i) => (
                <tr key={i}><td>{k}</td><td>{v ?? "—"}</td></tr>
              ))}
            </tbody>
          </table>
          <div style={{ display: "flex", gap: 8 }}>
            <button className={styles["ptrace-json-btn"]} onClick={() => setShowJson((s) => !s)}>
              <Icon glyph="CurlyBraces" size={12} /> {showJson ? "Hide JSON" : "View JSON"}
            </button>
            {showJson && (
              <button className={styles["ptrace-json-btn"]} onClick={copyJson}>
                <Icon glyph={copied ? "Checkmark" : "Copy"} size={12} /> {copied ? "Copied" : "Copy"}
              </button>
            )}
          </div>
          {showJson && (
            <div className={styles["ptrace-json-wrap"]}>
              <Code language="json" copyButtonAppearance="none">{jsonText}</Code>
            </div>
          )}
          {stage.features && (
            <div className={styles["col-features-footer"]} style={{ marginTop: 16 }}>
              <span className={styles["col-features-label"]}>MongoDB Features In Use</span>
              <div>
                {stage.features.map((f) => (
                  <span className={styles["feature-tag"]} key={f}>{f}</span>
                ))}
              </div>
            </div>
          )}
        </>
      ) : (
        <div className={styles["ptrace-not-reached"]}>not reached</div>
      )}
    </div>
  );
}

export default function PaymentTrace({ initialPid = "" }) {
  const [pid, setPid] = useState("");
  const [state, setState] = useState({ kind: "empty" }); // empty|loading|notfound|error|data
  const [recent, setRecent] = useState([]); // last 5 payments to pick from
  const [recentOpen, setRecentOpen] = useState(true); // Recent Transactions panel collapse
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

  // Deep-linked from the Pipeline Monitor's "Trace transaction" chip: trace the
  // handed-off paymentId as soon as this view mounts.
  useEffect(() => {
    if (initialPid) {
      setRecentOpen(false);
      fetchTrace(initialPid);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPid]);

  // Load the 5 most recent payments so the user can trace one without typing.
  useEffect(() => {
    let alive = true;
    pipelineApi("transactions?limit=5").then(({ data }) => {
      if (alive && data?.items) setRecent(data.items);
    });
    return () => { alive = false; };
  }, []);

  const stages = state.kind === "data" ? buildStages(state.d) : null;

  // On a new trace, default the selection to the first stage (Payment).
  useEffect(() => {
    if (!stages) { setSelectedKey(null); return; }
    setSelectedKey(stages[0].key);
  }, [state]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedStage = stages?.find((s) => s.key === selectedKey) || null;

  return (
    <div className={styles["ptrace-root"]}>
      {/* Recent transactions — pick one to trace without typing an id */}
      <div className={styles.panel} style={{ marginBottom: 16 }}>
        <div
          className={styles["panel-header"]}
          style={{ cursor: "pointer" }}
          onClick={() => setRecentOpen((o) => !o)}
        >
          <Icon glyph={recentOpen ? "ChevronDown" : "ChevronRight"} size={16} />
          <span className={styles["panel-title"]} style={{ marginLeft: 4 }}>Recent Transactions</span>
          <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 8 }}>
            last 5 payments · click one to trace it end-to-end
          </span>
        </div>
        {recentOpen && (
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
        )}
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
