"use client";

// The Payments and Operations lenses: a filterable list on the left, the selected
// payment's lifecycle deep dive on the right. Both lenses are this component — Operations
// is the same surface narrowed to terminal-state payments (doc 16 R4).
import { useState } from "react";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import Banner from "@leafygreen-ui/banner";
import TextInput from "@leafygreen-ui/text-input";
import { Select, Option } from "@leafygreen-ui/select";

import styles from "./PaymentsWorkflow.module.css";
import StatusPill from "./StatusPill";
import PaymentDeepDive from "./PaymentDeepDive";
import { usePaymentsList, useWorkflowExceptions } from "@/lib/api/hooks";
import { workflowApi } from "@/lib/api/client";
import { fmtAmount, fmtWhen } from "@/lib/paymentsWorkflow/status";

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

/**
 * Global command-palette search (research §3.1 #2 / §4 top bar).
 *
 * One box, prefix-routed on Enter:
 *   PAY-   → deep-link straight to that payment's lifecycle (onJump).
 *   CUST-  → drive the existing customerId list filter (onFilterCustomer).
 *   TXN- / ACC- / NOTIF- → resolve to a parent paymentId via the backend
 *      `/workflow/resolve/{ref}` route, then deep-link. ACC- may match several payments;
 *      the service returns the most recent.
 */
function CommandSearch({ onJump, onFilterCustomer }) {
  const [q, setQ] = useState("");
  const [notice, setNotice] = useState(null);
  const [resolving, setResolving] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    const v = q.trim();
    if (!v) {
      setNotice(null);
      onFilterCustomer("");
      return;
    }
    const upper = v.toUpperCase();
    if (upper.startsWith("PAY-")) {
      setNotice(null);
      onJump(v);
      return;
    }
    if (upper.startsWith("CUST-")) {
      setNotice(null);
      onFilterCustomer(v);
      return;
    }
    if (
      upper.startsWith("TXN-") ||
      upper.startsWith("ACC-") ||
      upper.startsWith("NOTIF-")
    ) {
      setResolving(true);
      const { data, error } = await workflowApi(
        `resolve/${encodeURIComponent(v)}`
      );
      setResolving(false);
      if (error) {
        setNotice(`No payment found for ${v}.`);
        return;
      }
      if (data?.paymentId) {
        setNotice(null);
        onJump(data.paymentId);
        return;
      }
      setNotice(`No payment found for ${v}.`);
      return;
    }
    setNotice("Unrecognized — prefix with PAY-, CUST-, TXN-, ACC-, or NOTIF-.");
  };

  return (
    <div className={styles.commandSearch}>
      <form className={styles.commandForm} onSubmit={submit}>
        <span className={styles.commandIcon}>
          <Icon glyph="Search" size={16} />
        </span>
        <input
          className={styles.commandInput}
          placeholder="Search: PAY- · CUST- · TXN- · ACC- · NOTIF-"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          disabled={resolving}
          aria-label="Global payment search"
        />
      </form>
      {notice && <div className={styles.commandNotice}>{notice}</div>}
    </div>
  );
}

// The list's projection already carries `debtor`/`creditor` (name + accountId), so the
// human-relevant column is the counterparty, not the internal customerId. Primary = the
// beneficiary; the sub-line is the funding account the money leaves from.
function beneficiaryOf(p) {
  return {
    name: p.creditor?.name || p.creditor?.accountId || "—",
    sub: p.debtor?.accountId ? `from ${p.debtor.accountId}` : "",
  };
}

function PaymentsTable({ items, selectedPaymentId, onSelect }) {
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <colgroup>
          <col style={{ width: "34%" }} />
          <col style={{ width: "20%" }} />
          <col style={{ width: "16%" }} />
          <col style={{ width: "12%" }} />
          <col style={{ width: "8%" }} />
          <col style={{ width: "10%" }} />
        </colgroup>
        <thead>
          <tr>
            <th>Beneficiary</th>
            <th>Payment ID</th>
            <th>Created</th>
            <th className={styles.numeric}>Amount</th>
            <th>Rail</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {items.map((p) => {
            const active = p.paymentId === selectedPaymentId;
            const beneficiary = beneficiaryOf(p);
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
                <td>
                  <div className={styles.beneficiary} title={beneficiary.name}>
                    {beneficiary.name}
                  </div>
                  {beneficiary.sub && (
                    <div className={styles.beneficiarySub}>{beneficiary.sub}</div>
                  )}
                </td>
                <td className={styles.mono}>{p.paymentId}</td>
                <td className={styles.cellMuted}>{fmtWhen(p.createdAt)}</td>
                <td className={`${styles.numeric} ${styles.cellAmount}`}>
                  {fmtAmount(p.amount, p.currency)}
                </td>
                <td><span className={styles.railTag}>{p.rail || "—"}</span></td>
                <td><StatusPill status={p.status} /></td>
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

  const move = (delta) =>
    setFilters((f) => ({ ...f, skip: Math.max(0, f.skip + delta * PAGE_SIZE) }));
  const clearFilters = () =>
    setFilters({ status: "", rail: "", customerId: "", from: "", to: "", skip: 0 });
  const activeFilterCount = [filters.status, filters.rail, filters.customerId, filters.from, filters.to]
    .filter(Boolean).length;

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
          <div className={styles.panelHead}>
            <div className={styles.panelTitleRow}>
              <Icon glyph={exceptionsOnly ? "Warning" : "List"} size={16} />
              <span className={styles.panelTitle}>
                {exceptionsOnly ? "Exceptions" : "Payments"}
              </span>
            </div>
            <span className={styles.panelDesc}>
              {exceptionsOnly
                ? "Payments stopped in a terminal state that need intervention."
                : "Payment orders initiated and moving through the lifecycle."}
            </span>
          </div>
          <div className={styles.panelHeadRight}>
            <span className={styles.resultCount}>
              {loading ? "Loading…" : `${total} ${total === 1 ? "payment" : "payments"}`}
            </span>
            <Button size="small" leftGlyph={<Icon glyph="Refresh" />} onClick={onRefresh}>
              Refresh
            </Button>
          </div>
        </div>

        {!exceptionsOnly && (
          <div className={styles.panelBody}>
            <CommandSearch
              onJump={onSelect}
              onFilterCustomer={(v) =>
                setFilters((f) => ({ ...f, customerId: v, skip: 0 }))
              }
            />
            <div className={styles.filterBar}>
              <Filters value={filters} onChange={setFilters} />
              {activeFilterCount > 0 && (
                <Button size="xsmall" onClick={clearFilters}>
                  Clear filters
                </Button>
              )}
            </div>
          </div>
        )}

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
              <span className={styles.muted}>
                {items.length
                  ? `Showing ${filters.skip + 1}–${Math.min(filters.skip + PAGE_SIZE, total)} of ${total}`
                  : ""}
              </span>
              <div className={styles.pagerButtons}>
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
