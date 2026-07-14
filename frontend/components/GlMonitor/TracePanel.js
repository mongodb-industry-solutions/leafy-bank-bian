// Transaction trace panel — ported from trace.js. NOT rendered by GlMonitor
// (the static gl_monitor.html keeps this panel commented out); preserved here
// so the full reference survives and can be dropped back into the layout later.
import React, { useState } from "react";
import styles from "./GlMonitor.module.css";
import { Chip } from "./Bits";
import { fmt, fmtMinor, fmtDate } from "@/lib/glMonitor/format";
import { pipelineApi } from "@/lib/api/client";

function StageRow({ name, children, statusChip }) {
  return (
    <div className={styles["trace-stage"]}>
      <div className={styles["trace-stage-name"]}>{name}</div>
      <div className={styles["trace-stage-body"]}>{children}</div>
      <div className={styles["trace-status"]}>{statusChip}</div>
    </div>
  );
}

const notReached = (text) => <span className={styles["trace-not-reached"]}>{text}</span>;

export default function TracePanel() {
  const [pid, setPid] = useState("");
  const [state, setState] = useState({ kind: "empty" }); // empty | loading | notfound | error | data

  async function fetchTrace() {
    const id = pid.trim();
    if (!id) return;
    setState({ kind: "loading" });
    const { data, error } = await pipelineApi(`trace/${encodeURIComponent(id)}`);
    if (error) {
      setState(error.includes("404") ? { kind: "notfound", id } : { kind: "error", error });
      return;
    }
    setState({ kind: "data", d: data });
  }

  let body;
  if (state.kind === "empty") body = <div className={styles["empty-state"]}>Enter a paymentId to trace across all 4 stages</div>;
  else if (state.kind === "loading") body = <div className={styles["empty-state"]}>Tracing…</div>;
  else if (state.kind === "notfound") body = <div className={styles["empty-state"]}>Payment not found: {state.id}</div>;
  else if (state.kind === "error") body = <div className={styles["error-state"]}>Error: {state.error}</div>;
  else {
    const { transaction: tx, ledgerEvent: le, journalEntry: jn } = state.d;
    const sls = state.d.subLedgerEntries || [];
    body = (
      <>
        <StageRow name="① Transaction" statusChip={tx ? <Chip status={tx.status} /> : null}>
          {tx ? <><span className={styles["row-id"]}>{tx.paymentId}</span><br />${fmt(tx.amount)} · {tx.rail || "—"} · {fmtDate(tx.settledAt)}</> : notReached("not found")}
        </StageRow>
        <StageRow name="② Ledger Event" statusChip={le ? <Chip status={le.postingStatus} /> : null}>
          {le ? <><span className={styles["row-id"]}>{le.eventId}</span><br />{le.eventType || "—"} · {le.postingMode?.type || "—"}<br />DR {le.debitLeg?.glAccountCode || "—"} / CR {le.creditLeg?.glAccountCode || "—"} · {fmtMinor(le.debitLeg?.amount)}</> : notReached("not yet ingested")}
        </StageRow>
        <StageRow name="③ SubLedger" statusChip={sls.length ? <Chip status={sls.every((s) => s.journalEntryId) ? "POSTED" : "PENDING"} /> : null}>
          {sls.length ? sls.map((s, i) => (
            <React.Fragment key={i}>
              <Chip status={s.side} /> <span className={styles["row-id"]}>{s.subLedgerId}</span> acct {s.controlAccountCode} · {fmtMinor(s.amount)} · bal {fmtMinor(s.runningBalance)}<br />
            </React.Fragment>
          )) : notReached("not yet projected")}
        </StageRow>
        <StageRow name="④ Journal" statusChip={jn ? <Chip status={jn.status} /> : null}>
          {jn ? <><span className={styles["row-id"]}>{jn.journalId}</span><br />{jn.batchId || jn.sourceReference?.sourceId || "—"} · {(jn.entries || []).length} lines</> : notReached("not yet journaled")}
        </StageRow>
      </>
    );
  }

  return (
    <div className={styles.panel}>
      <div className={styles["panel-header"]}>
        <span className={styles["panel-title"]}>Transaction Trace</span>
      </div>
      <div className={styles["panel-body"]}>
        <div className={styles["trace-input-row"]}>
          <input
            className={styles["trace-input"]}
            type="text"
            placeholder="PAY-abc12345"
            value={pid}
            onChange={(e) => setPid(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") fetchTrace(); }}
          />
          <button className={`${styles.btn} ${styles["btn-primary"]}`} onClick={fetchTrace}>Search</button>
        </div>
        <div>{body}</div>
      </div>
    </div>
  );
}
