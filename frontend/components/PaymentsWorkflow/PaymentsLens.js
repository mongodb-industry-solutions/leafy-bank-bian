"use client";

// The Payments and Operations lenses: a filterable list on the left, the selected
// payment's lifecycle deep dive on the right. Both lenses are this component — Operations
// is the same surface narrowed to terminal-state payments (doc 16 R4).
import { useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import Banner from "@leafygreen-ui/banner";
import TextInput from "@leafygreen-ui/text-input";
import { Select, Option } from "@leafygreen-ui/select";

import styles from "./PaymentsWorkflow.module.css";
import PaymentDeepDive from "./PaymentDeepDive";
import { usePaymentsList, useWorkflowExceptions } from "@/lib/api/hooks";
import { statusBadgeVariant, fmtAmount, fmtWhen } from "@/lib/paymentsWorkflow/status";

const PAGE_SIZE = 25;

// Mirrors the spec's rail enum. Kept as a literal rather than fetched: it is a contract
// the backend validates against, not data.
const RAILS = ["INTERNAL", "WIRE", "ACH", "CARD", "RTP"];

const STATUSES = [
  "DRAFT", "INITIATED", "VALIDATED", "ENRICHED", "FINAL_VALIDATED", "ROUTED",
  "AUTHORISED", "APPROVED", "SUBMITTED", "IN_PROGRESS", "POSTED", "SETTLED",
  "RECONCILED", "REJECTED", "FAILED", "RETURNED", "CANCELLED", "REVERSED", "REFUNDED",
];

function Filters({ value, onChange }) {
  const set = (k, v) => onChange({ ...value, [k]: v, skip: 0 });
  return (
    <div className={styles.filters}>
      <div className={styles.filterField}>
        <Select
          label="Status"
          size="small"
          placeholder="All"
          value={value.status ?? ""}
          onChange={(v) => set("status", v)}
        >
          {STATUSES.map((s) => (
            <Option key={s} value={s}>{s}</Option>
          ))}
        </Select>
      </div>
      <div className={styles.filterField}>
        <Select
          label="Rail"
          size="small"
          placeholder="All"
          value={value.rail ?? ""}
          onChange={(v) => set("rail", v)}
        >
          {RAILS.map((r) => (
            <Option key={r} value={r}>{r}</Option>
          ))}
        </Select>
      </div>
      <div className={styles.filterField}>
        <TextInput
          label="Customer ID"
          sizeVariant="small"
          placeholder="CUST-…"
          value={value.customerId ?? ""}
          onChange={(e) => set("customerId", e.target.value)}
        />
      </div>
      <div className={styles.filterField}>
        <TextInput
          label="From"
          type="date"
          sizeVariant="small"
          value={value.from ?? ""}
          onChange={(e) => set("from", e.target.value)}
        />
      </div>
      <div className={styles.filterField}>
        <TextInput
          label="To"
          type="date"
          sizeVariant="small"
          value={value.to ?? ""}
          onChange={(e) => set("to", e.target.value)}
        />
      </div>
    </div>
  );
}

function PaymentsTable({ items, selectedPaymentId, onSelect }) {
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Payment ID</th>
            <th>Created</th>
            <th>Customer</th>
            <th className={styles.numeric}>Amount</th>
            <th>Rail</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {items.map((p) => {
            const active = p.paymentId === selectedPaymentId;
            return (
              <tr
                key={p.paymentId}
                className={`${styles.row} ${active ? styles.rowActive : ""}`}
                onClick={() => onSelect(p.paymentId)}
                // Keyboard parity: the row is the control, so it must be reachable and
                // activatable without a pointer.
                tabIndex={0}
                role="button"
                aria-pressed={active}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelect(p.paymentId);
                  }
                }}
              >
                <td className={styles.mono}>{p.paymentId}</td>
                <td>{fmtWhen(p.createdAt)}</td>
                <td>{p.customerId || "—"}</td>
                <td className={styles.numeric}>{fmtAmount(p.amount, p.currency)}</td>
                <td>{p.rail || "—"}</td>
                <td>
                  <Badge variant={statusBadgeVariant(p.status)}>{p.status || "—"}</Badge>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function PaymentsLens({
  exceptionsOnly,
  refreshKey,
  onRefresh,
  selectedPaymentId,
  onSelect,
}) {
  const [filters, setFilters] = useState({
    status: "", rail: "", customerId: "", from: "", to: "", skip: 0,
  });

  // Two hooks, one used — the Operations lens has its own endpoint (terminal states are
  // derived from the state machine server-side, not restated as a client filter).
  const listed = usePaymentsList(
    { ...filters, limit: PAGE_SIZE, skip: filters.skip },
    refreshKey
  );
  const excepted = useWorkflowExceptions({ limit: PAGE_SIZE, skip: filters.skip }, refreshKey);
  const { items, total, loading, error } = exceptionsOnly ? excepted : listed;

  const page = Math.floor(filters.skip / PAGE_SIZE) + 1;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const move = (delta) =>
    setFilters((f) => ({ ...f, skip: Math.max(0, f.skip + delta * PAGE_SIZE) }));

  // Selecting a payment ADVANCES to the lifecycle in place, rather than appending it below
  // the table. Appending pushed the rail off the bottom of a full page of rows, so reading
  // a payment's trace began with a scroll. The two views are steps in one flow, not two
  // panels, so they share the same real estate and `Back` returns to the list with the
  // selection, filters and page intact.
  if (selectedPaymentId) {
    return (
      <div className={styles.stack}>
        <PaymentDeepDive
          paymentId={selectedPaymentId}
          refreshKey={refreshKey}
          onBack={() => onSelect(null)}
        />
      </div>
    );
  }

  return (
    <div className={styles.stack}>
      <div className={styles.panel}>
        <div className={styles.panelHeader}>
          <Icon glyph={exceptionsOnly ? "Warning" : "List"} size={16} />
          <span className={styles.panelTitle}>
            {exceptionsOnly ? "Exceptions" : "Payments"}
          </span>
          <span className={styles.muted} style={{ marginLeft: 8 }}>
            {loading ? "loading…" : `${total} total`}
          </span>
          <div style={{ marginLeft: "auto" }}>
            <Button size="xsmall" leftGlyph={<Icon glyph="Refresh" />} onClick={onRefresh}>
              Refresh
            </Button>
          </div>
        </div>

        <div className={styles.panelBody}>
          {exceptionsOnly ? (
            <span className={styles.muted}>
              Payments that stopped in a terminal state and need intervention.
            </span>
          ) : (
            <Filters value={filters} onChange={setFilters} />
          )}
        </div>

        {error && (
          <div className={styles.panelBody}>
            <Banner variant="danger">Could not load payments — {error}</Banner>
          </div>
        )}

        {!error && loading && <div className={styles.emptyState}>Loading payments…</div>}

        {!error && !loading && items.length === 0 && (
          <div className={styles.emptyState}>
            {exceptionsOnly
              ? "No payments need intervention."
              : "No payments match these filters."}
          </div>
        )}

        {!error && !loading && items.length > 0 && (
          <>
            <PaymentsTable
              items={items}
              selectedPaymentId={selectedPaymentId}
              onSelect={onSelect}
            />
            <div className={styles.pager}>
              <span className={styles.muted}>Page {page} of {pages}</span>
              <div style={{ display: "flex", gap: 8 }}>
                <Button size="xsmall" disabled={filters.skip === 0} onClick={() => move(-1)}>
                  Previous
                </Button>
                <Button
                  size="xsmall"
                  disabled={filters.skip + PAGE_SIZE >= total}
                  onClick={() => move(1)}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </div>

    </div>
  );
}
