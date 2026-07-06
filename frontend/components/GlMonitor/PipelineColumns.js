import React from "react";
import styles from "./GlMonitor.module.css";
import { Chip, VariantChip, BatchChip } from "./Bits";
import { fmt, fmtMinor, fmtDate } from "@/lib/glMonitor/format";

// Renders feed rows with the "current ↑ · ↓ last batch" separator when the
// tag transitions from CURRENT to LAST (ported from batch.js renderWithSep).
function FeedRows({ col, items, selected, onSelect, renderRow, rowId }) {
  const out = [];
  let prevTag = null;
  items.forEach((item, i) => {
    if (item._batchTag === "LAST" && prevTag === "CURRENT") {
      out.push(
        <div className={styles["batch-sep"]} key={`sep-${i}`}>current ↑  ·  ↓ last batch</div>
      );
    }
    const isSel = selected.col === col && selected.idx === i;
    out.push(
      <div
        className={`${styles["feed-row"]}${isSel ? " " + styles.selected : ""}`}
        key={i}
        id={rowId ? rowId(item) : undefined}
        onClick={() => onSelect(col, i, item)}
      >
        {renderRow(item)}
      </div>
    );
    prevTag = item._batchTag;
  });
  return out;
}

// features: array of MongoDB feature-tag labels for the footer (null = no footer).
function Column({ headerBg, stageCls, stageLabel, title, sub, count, state, features, col, selected, onSelect, renderRow, emptyMsg, rowId }) {
  let body;
  if (state.loading) body = <div className={styles["empty-state"]}>Loading…</div>;
  else if (state.error) body = <div className={styles["error-state"]}>Error: {state.error}</div>;
  else if (!state.items.length) body = <div className={styles["empty-state"]}>{emptyMsg}</div>;
  else body = <FeedRows col={col} items={state.items} selected={selected} onSelect={onSelect} renderRow={renderRow} rowId={rowId} />;

  return (
    <div className={styles.col}>
      <div className={styles["col-header"]} style={{ background: headerBg }}>
        <div className={styles["col-header-main"]}>
          <div>
            <div className={styles["row-top"]}>
              <span className={`${styles["col-stage"]} ${styles[stageCls]}`}>{stageLabel}</span>
              <span className={styles["col-title"]}>{title}</span>
            </div>
            <div className={styles["col-sub"]}>{sub}</div>
          </div>
          <span className={styles["col-count"]}>{count}</span>
        </div>
      </div>
      <div className={styles["col-body"]}>{body}</div>
      {features && (
        <div className={styles["col-features-footer"]}>
          <span className={styles["col-features-label"]}>MongoDB Features In Use</span>
          <div>
            {features.map((f) => (
              <span className={styles["feature-tag"]} key={f}>{f}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function PipelineColumns({ columns, selected, onSelect, onJumpToLe }) {
  return (
    <div className={styles.pipeline}>
      <Column
        headerBg="var(--col1)" stageCls="stage-tx" stageLabel="upstream" title="Transactions"
        sub="settled payments · read-only" count={columns.tx.count} state={columns.tx}
        features={null} col="tx" selected={selected} onSelect={onSelect}
        emptyMsg="No transactions in last 2 batch windows"
        renderRow={(t) => (
          <>
            <div className={styles["row-top"]}>
              <span className={styles["row-id"]}>{t.paymentId || "—"}</span>
              <Chip status={t.transactionStatus} />
              <BatchChip tag={t._batchTag} />
            </div>
            <div className={styles["row-detail"]}>${fmt(t.amount)} · {t.rail || "—"} · {t.paymentType || "—"}</div>
            <div className={styles["row-sub"]}>{fmtDate(t.createdAt)} · {t.payer?.accountId || "—"} → {t.payee?.accountId || "—"}</div>
            <div className={styles["row-sub"]} style={{ marginTop: 3 }}>
              {t.ledgerEventId
                ? <span
                    className={styles["le-link-chip"]}
                    title="Jump to this Ledger Event"
                    onClick={(ev) => { ev.stopPropagation(); onJumpToLe?.(t.ledgerEventId); }}
                  >→ {t.ledgerEventId}</span>
                : <span className={styles["le-not-ingested"]}>LE: not yet ingested</span>}
            </div>
          </>
        )}
      />

      <Column
        headerBg="var(--col2)" stageCls="stage-le" stageLabel="Stage ①" title="Ledger Events"
        sub="ingest_worker · change stream" count={columns.le.count} state={columns.le}
        features={["Change Streams", "Resume Tokens", "DuplicateKeyError idempotency", "$jsonSchema validator"]}
        col="le" selected={selected} onSelect={onSelect} rowId={(e) => `le-row-${e.eventId}`}
        emptyMsg="No ledger events in last 2 batch windows"
        renderRow={(e) => (
          <>
            <div className={styles["row-top"]}>
              <span className={styles["row-id"]}>{e.eventId || "—"}</span>
              <Chip status={e.postingStatus} />
              <BatchChip tag={e._batchTag} />
            </div>
            <div className={styles["row-detail"]}>{e.eventType || "—"} · {e.postingMode?.type || "—"}</div>
            <div className={styles["row-sub"]}>{fmtDate(e.occurredAt)} · v{e.mappingVersion || "?"}</div>
          </>
        )}
      />

      <Column
        headerBg="var(--col3)" stageCls="stage-sl" stageLabel="Stage ②" title="SubLedger Entries"
        sub="projection_worker · ACID txn" count={columns.sl.count} state={columns.sl}
        features={["Change Streams", "ACID multi-doc transaction", "$jsonSchema validator", "Partial index (journalEntryId ≠ \"\")"]}
        col="sl" selected={selected} onSelect={onSelect}
        emptyMsg="No subledger entries in last 2 batch windows"
        renderRow={(s) => (
          <>
            <div className={styles["row-top"]}>
              <Chip status={s.side} />
              <span className={styles["row-id"]}>{s.subLedgerId || "—"}</span>
              <BatchChip tag={s._batchTag} />
            </div>
            <div className={styles["row-detail"]}>acct {s.controlAccountCode || "—"} · {fmtMinor(s.amount)} · bal {fmtMinor(s.runningBalance)}</div>
            <div className={styles["row-sub"]}>
              {s.journalEntryId
                ? <span style={{ color: "var(--green)" }}>✓ {s.journalEntryId}</span>
                : <span style={{ color: "var(--yellow)" }}>⏳ unjournaled</span>}
            </div>
          </>
        )}
      />

      <Column
        headerBg="var(--col4)" stageCls="stage-jn" stageLabel="Stage ③" title="Journal Entries"
        sub="gl_batch · scheduled" count={columns.jn.count} state={columns.jn}
        features={["Aggregation ($group $sum $addToSet)", "ACID transaction", "$expr Pacioli validator", "Multikey index (entries.accountCode)"]}
        col="jn" selected={selected} onSelect={onSelect}
        emptyMsg="No journal entries in last 2 batch windows"
        renderRow={(j) => {
          const entries = j.entries || [];
          const debit = entries.filter((e) => e.side === "DEBIT").reduce((a, e) => a + (e.amount || 0), 0);
          const credit = entries.filter((e) => e.side === "CREDIT").reduce((a, e) => a + (e.amount || 0), 0);
          const balanced = debit === credit;
          return (
            <>
              <div className={styles["row-top"]}>
                <span className={styles["row-id"]}>{j.journalId || "—"}</span>
                <Chip status={j.status} />
                {balanced
                  ? <span style={{ fontSize: 12 }} title="Balanced">⚖️</span>
                  : <span style={{ color: "var(--red)" }}>⚠ unbalanced</span>}
                <BatchChip tag={j._batchTag} />
              </div>
              <div className={styles["row-detail"]}>{j.batchId || j.sourceReference?.sourceId || "—"} · {j.periodCode || "—"}</div>
              <div className={styles["row-sub"]}>{entries.length} lines · DR {fmtMinor(debit)} / CR {fmtMinor(credit)}</div>
            </>
          );
        }}
      />
    </div>
  );
}
