import React from "react";
import styles from "./GlMonitor.module.css";
import { Chip, KvTable } from "./Bits";
import { fmt, fmtMinor, fmtDate } from "@/lib/glMonitor/format";

// detail: { open, title, bg, kind: "tx"|"le"|"sl"|"jn", data }. Mirrors
// detail.js showTx/Le/Sl/JnDetail — closed state renders nothing.
export default function DetailPanel({ detail, onClose }) {
  if (!detail.open) return null;
  return (
    <div className={styles["detail-wrap"]}>
      <div className={styles["detail-panel"]}>
        <div className={styles["detail-header"]} style={{ background: detail.bg }}>
          <span className={styles["detail-title"]}>{detail.title}</span>
          <span className={styles["detail-close"]} onClick={onClose}>✕</span>
        </div>
        <div>{renderContent(detail.kind, detail.data)}</div>
      </div>
    </div>
  );
}

function renderContent(kind, data) {
  if (kind === "tx") return <TxDetail t={data} />;
  if (kind === "le") return <LeDetail e={data} />;
  if (kind === "sl") return <SlDetail s={data} />;
  if (kind === "jn") return <JnDetail j={data} />;
  return null;
}

function TxDetail({ t }) {
  return (
    <div className={styles["detail-body"]}>
      <div className={styles["detail-section"]}>
        <div className={styles["detail-section-title"]}>Payment</div>
        <KvTable pairs={[
          ["paymentId", t.paymentId],
          ["amount", `$${fmt(t.amount)} ${t.currency || "USD"}`],
          ["rail", t.rail],
          ["paymentType", t.paymentType],
          ["transactionStatus", <Chip status={t.transactionStatus} key="c" />],
          ["createdAt", fmtDate(t.createdAt)],
        ]} />
      </div>
      <div className={styles["detail-section"]}>
        <div className={styles["detail-section-title"]}>Parties</div>
        <KvTable pairs={[
          ["payer.accountId", t.payer?.accountId],
          ["payee.accountId", t.payee?.accountId],
        ]} />
      </div>
    </div>
  );
}

function LeDetail({ e }) {
  const dr = e.debitLeg || {};
  const cr = e.creditLeg || {};
  const pr = e.postingResult || {};
  return (
    <div className={styles["detail-body"]}>
      <div className={styles["detail-section"]}>
        <div className={styles["detail-section-title"]}>Event</div>
        <KvTable pairs={[
          ["eventId", e.eventId],
          ["idempotencyKey", e.idempotencyKey],
          ["groupId", e.groupId],
          ["occurredAt", e.occurredAt],
          ["periodCode", e.meta?.periodCode],
          ["eventType", e.eventType],
          ["postingMode", e.postingMode?.type],
          ["mappingVersion", e.mappingVersion],
          ["postingStatus", <Chip status={e.postingStatus} key="c" />],
        ]} />
        <div className={styles["detail-section-title"]} style={{ marginTop: 12 }}>Posting Result</div>
        <KvTable pairs={[
          ["subLedgerIdDebit", pr.subLedgerIdDebit],
          ["subLedgerIdCredit", pr.subLedgerIdCredit],
          ["journalEntryId", pr.journalEntryId],
          ["postedAt", pr.postedAt],
        ]} />
      </div>
      <div className={styles["detail-section"]}>
        <div className={styles["detail-section-title"]}>Debit Leg</div>
        <KvTable pairs={[
          ["glAccountCode", dr.glAccountCode],
          ["controlAccountCode", dr.controlAccountCode],
          ["amount", fmtMinor(dr.amount)],
          ["currency", dr.currency],
          ["entityType", dr.entityReference?.entityType],
          ["entityId", dr.entityReference?.entityId],
        ]} />
        <div className={styles["detail-section-title"]} style={{ marginTop: 12 }}>Credit Leg</div>
        <KvTable pairs={[
          ["glAccountCode", cr.glAccountCode],
          ["controlAccountCode", cr.controlAccountCode],
          ["amount", fmtMinor(cr.amount)],
          ["currency", cr.currency],
          ["entityType", cr.entityReference?.entityType],
          ["entityId", cr.entityReference?.entityId],
        ]} />
        {dr.amount != null && cr.amount != null && (
          <div style={{ marginTop: 8, fontSize: 11 }}>
            {dr.amount === cr.amount
              ? <span style={{ color: "var(--green)" }}>✓ ΣDEBIT == ΣCREDIT ({dr.amount})</span>
              : <span style={{ color: "var(--red)" }}>⚠ UNBALANCED: DR {dr.amount} ≠ CR {cr.amount}</span>}
          </div>
        )}
      </div>
    </div>
  );
}

function SlDetail({ s }) {
  return (
    <div className={styles["detail-body"]}>
      <div className={styles["detail-section"]}>
        <div className={styles["detail-section-title"]}>Entry</div>
        <KvTable pairs={[
          ["subLedgerId", s.subLedgerId],
          ["idempotencyKey", s.idempotencyKey],
          ["controlAccountCode", s.controlAccountCode],
          ["subLedgerType", s.subLedgerType],
          ["side", <Chip status={s.side} key="c" />],
          ["amount", fmtMinor(s.amount)],
          ["currency", s.currency],
          ["periodCode", s.periodCode],
          ["status", <Chip status={s.status} key="c2" />],
          ["runningBalance", fmtMinor(s.runningBalance)],
        ]} />
      </div>
      <div className={styles["detail-section"]}>
        <div className={styles["detail-section-title"]}>Dates & References</div>
        <KvTable pairs={[
          ["valueDate", s.valueDate],
          ["postingDate", s.postingDate],
          ["journalEntryId", s.journalEntryId || "⏳ unjournaled"],
          ["entityType", s.entityReference?.entityType],
          ["entityId", s.entityReference?.entityId],
          ["sourceId", s.sourceReference?.sourceId],
        ]} />
      </div>
    </div>
  );
}

function JnDetail({ j }) {
  const entries = j.entries || [];
  const debit = entries.filter((e) => e.side === "DEBIT").reduce((a, e) => a + (e.amount || 0), 0);
  const credit = entries.filter((e) => e.side === "CREDIT").reduce((a, e) => a + (e.amount || 0), 0);
  return (
    <div className={styles["detail-body"]}>
      <div className={styles["detail-section"]}>
        <div className={styles["detail-section-title"]}>Journal</div>
        <KvTable pairs={[
          ["journalId", j.journalId],
          ["idempotencyKey", j.idempotencyKey],
          ["periodCode", j.periodCode],
          ["batchId", j.batchId || j.sourceReference?.sourceId],
          ["journalType", j.journalType],
          ["status", <Chip status={j.status} key="c" />],
          ["currency", j.currency],
          ["totalAmount", fmtMinor(j.totalAmount)],
          ["txnCount", j.txnCount ?? j.sourceReference?.txnCount],
          ["createdBy", j.createdBy],
          ["postingDate", j.postingDate],
          ["createdAt", j.createdAt],
        ]} />
      </div>
      <div className={styles["detail-section"]}>
        <div className={styles["detail-section-title"]}>Journal Lines</div>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border)" }}>
              <th style={{ textAlign: "left", padding: "3px 6px", color: "var(--text-muted)" }}>#</th>
              <th style={{ textAlign: "left", padding: "3px 6px", color: "var(--text-muted)" }}>Acct</th>
              <th style={{ textAlign: "left", padding: "3px 6px", color: "var(--text-muted)" }}>Name</th>
              <th style={{ textAlign: "left", padding: "3px 6px", color: "var(--text-muted)" }}>Side</th>
              <th style={{ textAlign: "right", padding: "3px 6px", color: "var(--text-muted)" }}>Amount</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((l, i) => (
              <tr key={i}>
                <td>{l.lineNumber}</td>
                <td><span style={{ fontFamily: "monospace" }}>{l.accountCode}</span></td>
                <td>{l.accountName || "—"}</td>
                <td><Chip status={l.side} /></td>
                <td style={{ fontFamily: "monospace", textAlign: "right" }}>{fmtMinor(l.amount)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr style={{ borderTop: "2px solid var(--border)", fontWeight: 700 }}>
              <td colSpan={4} style={{ padding: "4px 6px", fontSize: 11 }}>
                {debit === credit
                  ? <span style={{ color: "var(--green)" }}>⚖️ ΣDEBIT == ΣCREDIT ({fmtMinor(debit)})</span>
                  : <span style={{ color: "var(--red)" }}>⚠ UNBALANCED: DR {fmtMinor(debit)} ≠ CR {fmtMinor(credit)}</span>}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}
