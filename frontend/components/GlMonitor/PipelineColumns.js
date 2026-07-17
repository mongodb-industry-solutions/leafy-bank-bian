import React, { useRef, useLayoutEffect } from "react";
import styles from "./GlMonitor.module.css";
import { Chip, VariantChip, BalanceChip } from "./Bits";
import { fmt, fmtMinor, fmtDate } from "@/lib/glMonitor/format";
import { linkKeyOf } from "@/lib/glMonitor/lineage";

// Renders feed rows with the "current ↑ · ↓ last batch" separator when the
// tag transitions from CURRENT to LAST (ported from batch.js renderWithSep).
function FeedRows({ col, items, selected, lineage, onSelect, renderRow, rowId, keyOf }) {
  const out = [];
  let prevTag = null;
  const linkedSet = lineage?.[col];
  const active = !!lineage;
  items.forEach((item, i) => {
    const key = keyOf ? keyOf(item, i) : i;
    if (item._batchTag === "LAST" && prevTag === "CURRENT") {
      out.push(
        <div className={styles["batch-sep"]} key={`sep-${key}`}>current ↑  ·  ↓ last 2 batches</div>
      );
    }
    const isSel = selected.col === col && selected.idx === i;
    const isLinked = !isSel && linkedSet?.has(linkKeyOf[col]?.(item));
    const isDimmed = active && !isSel && !isLinked;
    const cls = `${styles["feed-row"]}${isSel ? " " + styles.selected : ""}${isLinked ? " " + styles.linked : ""}${isDimmed ? " " + styles.dimmed : ""}`;
    out.push(
      <div
        className={cls}
        key={key}
        id={rowId ? rowId(item) : undefined}
        data-linked={isLinked ? "1" : undefined}
        data-selected={isSel ? "1" : undefined}
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
function Column({ headerBg, stageCls, stageLabel, title, sub, count, state, features, col, selected, lineage, onSelect, renderRow, emptyMsg, rowId, keyOf, bodyRef }) {
  let body;
  if (state.loading) body = <div className={styles["empty-state"]}>Loading…</div>;
  else if (state.error) body = <div className={styles["error-state"]}>Error: {state.error}</div>;
  else if (!state.items.length) body = <div className={styles["empty-state"]}>{emptyMsg}</div>;
  else body = <FeedRows col={col} items={state.items} selected={selected} lineage={lineage} onSelect={onSelect} renderRow={renderRow} rowId={rowId} keyOf={keyOf} />;

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
      <div className={styles["col-body"]} ref={bodyRef}>{body}</div>
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

// Scroll a column body so its linked/selected rows sit centered in view, so the
// whole highlighted chain is visible across all four columns after a click.
function centerLinkedRows(body) {
  if (!body) return;
  const rows = body.querySelectorAll('[data-linked="1"],[data-selected="1"]');
  if (!rows.length) return;
  const bodyRect = body.getBoundingClientRect();
  let min = Infinity;
  let max = -Infinity;
  rows.forEach((r) => {
    const rect = r.getBoundingClientRect();
    const top = rect.top - bodyRect.top + body.scrollTop;
    min = Math.min(min, top);
    max = Math.max(max, top + rect.height);
  });
  const target = (min + max) / 2 - body.clientHeight / 2;
  const maxScroll = body.scrollHeight - body.clientHeight;
  body.scrollTo({ top: Math.min(Math.max(0, target), maxScroll), behavior: "smooth" });
}

export default function PipelineColumns({ columns, selected, lineage, onSelect, onTraceTx }) {
  const bodyRefs = { tx: useRef(null), le: useRef(null), sl: useRef(null), jn: useRef(null) };

  // After a selection resolves a lineage, bring each column's linked rows into
  // view together. Keyed on the click, not on lineage identity (recomputed each render).
  useLayoutEffect(() => {
    if (!lineage) return;
    Object.values(bodyRefs).forEach((ref) => centerLinkedRows(ref.current));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected.col, selected.idx, !!lineage]);

  return (
    <div className={styles.pipeline}>
      <Column
        headerBg="var(--col1)" stageCls="stage-tx" stageLabel="upstream" title="Transactions"
        sub="settled payments · read-only" count={columns.tx.count} state={columns.tx}
        features={null} col="tx" selected={selected} lineage={lineage} onSelect={onSelect}
        bodyRef={bodyRefs.tx}
        keyOf={(t, i) => t.paymentId ?? i}
        emptyMsg="No transactions in last 2 batch windows"
        renderRow={(t) => (
          <>
            <div className={styles["row-top"]}>
              <span className={styles["row-id"]}>{t.paymentId || "—"}</span>
              <Chip status={t.transactionStatus} />
            </div>
            <div className={styles["row-detail"]}>${fmt(t.amount)} · {t.rail || "—"} · {t.paymentType || "—"}</div>
            <div className={styles["row-sub"]}>{fmtDate(t.createdAt)} · {t.payer?.accountId || "—"} → {t.payee?.accountId || "—"}</div>
            <div className={styles["row-sub"]} style={{ marginTop: 3 }}>
              <span
                className={styles["le-link-chip"]}
                title="Trace this payment end-to-end"
                onClick={(ev) => { ev.stopPropagation(); onTraceTx?.(t.paymentId); }}
              >→ Trace transaction</span>
            </div>
          </>
        )}
      />

      <Column
        headerBg="var(--col2)" stageCls="stage-le" stageLabel="Stage ①" title="Ledger Events"
        sub="ingest_worker · change stream" count={columns.le.count} state={columns.le}
        features={["Change Streams", "Resume Tokens", "DuplicateKeyError idempotency", "$jsonSchema validator"]}
        col="le" selected={selected} lineage={lineage} onSelect={onSelect} rowId={(e) => `le-row-${e.eventId}`}
        bodyRef={bodyRefs.le}
        keyOf={(e, i) => e.eventId ?? i}
        emptyMsg="No ledger events in last 2 batch windows"
        renderRow={(e) => (
          <>
            <div className={styles["row-top"]}>
              <span className={styles["row-id"]}>{e.eventId || "—"}</span>
              <Chip status={e.postingStatus} />
              {e.debitLeg && e.creditLeg && (
                <BalanceChip balanced={e.debitLeg.amount === e.creditLeg.amount} />
              )}
            </div>
            <div className={styles["row-detail"]}>{e.eventType || "—"} · {e.postingMode?.type || "—"} · {fmtMinor(e.debitLeg?.amount ?? e.creditLeg?.amount)}</div>
            <div className={styles["row-sub"]}>{fmtDate(e.occurredAt)} · v{e.mappingVersion || "?"}</div>
          </>
        )}
      />

      <Column
        headerBg="var(--col3)" stageCls="stage-sl" stageLabel="Stage ②" title="SubLedger Entries"
        sub="projection_worker · ACID txn" count={columns.sl.count} state={columns.sl}
        features={["Change Streams", "ACID multi-doc transaction", "$jsonSchema validator", "Partial index (journalEntryId ≠ \"\")"]}
        col="sl" selected={selected} lineage={lineage} onSelect={onSelect}
        bodyRef={bodyRefs.sl}
        keyOf={(s, i) => s.subLedgerId ?? i}
        emptyMsg="No subledger entries in last 2 batch windows"
        renderRow={(s) => (
          <>
            <div className={styles["row-top"]}>
              <Chip status={s.side} />
              <span className={styles["row-id"]}>{s.subLedgerId || "—"}</span>
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
        col="jn" selected={selected} lineage={lineage} onSelect={onSelect}
        bodyRef={bodyRefs.jn}
        keyOf={(j, i) => j.journalId ?? i}
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
                <BalanceChip balanced={balanced} />
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
