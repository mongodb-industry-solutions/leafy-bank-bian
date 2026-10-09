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
import GenerateIncomingWireModal from "./GenerateIncomingWireModal";
import { usePaymentsList } from "@/lib/api/hooks";
import { workflowApi } from "@/lib/api/client";
import { fmtAmount, fmtWhen } from "@/lib/paymentsWorkflow/status";

const PAGE_SIZE = 10;

// Mirrors the spec's rail enum. Kept as a literal rather than fetched: it is a contract
// the backend validates against, not data.
const RAILS = ["INTERNAL", "WIRE", "ACH", "CARD", "RTP"];

// `Outgoing` includes payments written before the incoming wire, which carry no `direction`
// (the backend filters OUTBOUND as "not INBOUND").
const DIRECTIONS = [
  { value: "INBOUND", label: "Incoming" },
  { value: "OUTBOUND", label: "Outgoing" },
];

// Stage 3's corridor determination. Bank-internal transfers count as Domestic.
const CORRIDORS = [
  { value: "DOMESTIC", label: "Domestic" },
  { value: "INTERNATIONAL", label: "International" },
];

const STATUSES = [
  "DRAFT", "INITIATED", "VALIDATED", "ENRICHED", "FINAL_VALIDATED", "ROUTED",
  "MANUAL_FRAUD_REVIEW", "AUTHORISED", "APPROVED", "SUBMITTED", "IN_PROGRESS", "POSTED", "SETTLED",
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
        <Select
          label="Direction"
          size="small"
          placeholder="All"
          value={value.direction ?? ""}
          onChange={(v) => set("direction", v)}
        >
          {DIRECTIONS.map((d) => (
            <Option key={d.value} value={d.value}>{d.label}</Option>
          ))}
        </Select>
      </div>
      <div className={styles.filterField}>
        <Select
          label="Domestic / International"
          size="small"
          placeholder="All"
          value={value.corridor ?? ""}
          onChange={(v) => set("corridor", v)}
        >
          {CORRIDORS.map((c) => (
            <Option key={c.value} value={c.value}>{c.label}</Option>
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
          <Icon glyph="MagnifyingGlass" size={16} />
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
// human-relevant column is the counterparty, not the internal customerId — and the
// counterparty is decided by DIRECTION, because the two directions face opposite ways:
//   outbound: the external recipient (creditor), funded from the debtor's account
//   inbound:  the external SENDER (debtor), landing in our customer's account
// Showing the creditor on an inbound row would name our own customer as the
// "counterparty" of a wire they received — the interesting party is who sent it.
//
// ⚠️ Absent `direction` = outbound: every payment created before the incoming flow
// existed is outbound, and the field was absent then. Never read absence as "unknown".
function beneficiaryOf(p) {
  if (p.subjectRef?.kind === "STATEMENT_LINE") {
    return { name: "Correspondent statement line", sub: `${p.subjectRef.paymentMessageId} · line ${p.subjectRef.lineNo}` };
  }
  if (p.direction === "INBOUND") {
    return {
      name: p.debtor?.name || p.debtor?.bankName || "External sender",
      sub: p.creditor?.accountId ? `to ${p.creditor.accountId}` : "",
    };
  }
  return {
    name: p.creditor?.name || p.creditor?.accountId || "—",
    sub: p.debtor?.accountId ? `from ${p.debtor.accountId}` : "",
  };
}

// The three status axes are independent (defect 2026-09-08 `discriminator-conflation`):
// `status` = money moved, `postingStatus` = accounting (lags via the async GL batch),
// `settlementStatus` = external settlement. The list's single `status` pill reads as
// "done" when the other two lag — Doina (Sep 17): "many transactions show SETTLED before
// settlement is even initiated." Surface posting + settlement as a muted subline so a
// settled-but-unposted payment reads "posting pending · settlement —" instead of just
// SETTLED. Reconciliation stays in the deep-dive (terminal, not a scan axis).
// Stage 9 — the Operations queue row shows the exception reason + the discrepancy line
// (R15's mockup: "Payment $25,000 / Settlement $24,975 — $25 discrepancy"). The joined
// `exception` doc (doc 24 §3 step 7) carries the category + detail; render it as a subline
// under the status pill so the analyst sees WHY the payment is queued at a glance.
function fmtDiscrepancy(amount) {
  if (amount == null) return "";
  // detail amounts are major units (float dollars) — show whole-dollar when integral
  const n = Number(amount);
  return Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`;
}
const humanize = (code) => {
  const t = String(code || "").toLowerCase().replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
};
function exceptionLine(exc) {
  if (!exc) return "";
  const d = exc.detail || {};
  if (exc.category === "ORPHANED_SETTLEMENT") {
    return `Orphaned settlement · statement ref ${d.reference || "—"}`;
  }
  if (exc.category === "DUPLICATE_SIGNAL" && d.duplicateOf) {
    return `Duplicate signal · resembles ${d.duplicateOf}`;
  }
  const disc = fmtDiscrepancy(d.discrepancyAmount);
  if (disc) {
    return `${humanize(exc.category)} · ${disc} discrepancy`;
  }
  if (d.returnCode) {
    return `${humanize(exc.category)} · return ${d.returnCode}`;
  }
  return humanize(exc.category);
}

function resolvedLine(exc) {
  const action = exc.resolution?.action;
  return `${exc.status === "DISMISSED" ? "Dismissed" : "Resolved"}${
    action ? ` · ${ACTION_LABELS[action] || humanize(action)}` : ""
  }`;
}

// Friendly labels for a resolved exception's action — mirrors the map in PaymentDeepDive.
const ACTION_LABELS = {
  RETRY_SETTLEMENT: "Retry settlement",
  RETURN_FUNDS: "Return funds",
  ACCEPT_DISCREPANCY: "Accept discrepancy",
  DISMISS: "Dismiss",
  RECHECK: "Statement line arrived",
  REPAIR: "Repair",
  RETURN: "Return",
  POST_ADJUSTMENT: "Post adjustment",
};

/**
 * The manual incoming-wire trigger: opens the Generate Incoming Wire form, which simulates an
 * external pacs.008, runs it through the inbound lifecycle, and jumps to the new payment —
 * the same "watch it land" flow the outbound wizard's View-lifecycle gives.
 *
 * Reverses the 2026-09-29 "no scenario picker" decision (Kiran, 2026-10-08, after Doina's
 * Oct 6 review): the presenter now chooses wire type, amount, currencies, sending bank and
 * scenario. The 5-minute background simulator (`ENABLE_INBOUND_SIM`) is unchanged and stays
 * happy-path only.
 */
function InboundTrigger({ onRefresh, onJump }) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <Button size="small" leftGlyph={<Icon glyph="Import" />} onClick={() => setOpen(true)}>
        Generate incoming wire
      </Button>
      <GenerateIncomingWireModal
        open={open}
        onClose={() => setOpen(false)}
        onGenerated={(paymentId) => {
          setOpen(false);
          if (onRefresh) onRefresh();
          if (onJump) onJump(paymentId);
        }}
      />
    </>
  );
}

function PaymentsTable({ items, selectedPaymentId, onSelect }) {
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <colgroup>
          <col style={{ width: "28%" }} />
          <col style={{ width: "18%" }} />
          <col style={{ width: "16%" }} />
          <col style={{ width: "14%" }} />
          <col style={{ width: "8%" }} />
          <col style={{ width: "16%" }} />
        </colgroup>
        <thead>
          <tr>
            <th>Counterparty</th>
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
            const excOpen = p.exception && p.exception.status === "OPEN";
            const excClosed = p.exception && p.exception.status !== "OPEN";
            // A statement line with no payment (plan A3): there is no deep-dive to open.
            const isStatementLine = p.subjectRef?.kind === "STATEMENT_LINE";
            const open = () => { if (!isStatementLine) onSelect(p.paymentId); };
            return (
              <tr
                key={p.paymentId}
                className={`${styles.row} ${active ? styles.rowActive : ""}`}
                onClick={open}
                // Keyboard parity: the row is the control, so it must be reachable and
                // activatable without a pointer.
                tabIndex={0}
                role="button"
                aria-pressed={active}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    open();
                  }
                }}
              >
                <td>
                  <div className={styles.beneficiary} title={beneficiary.name}>
                    {/* The direction marker: at a glance, which way the money moved.
                        Inbound gets the accent green (money IN); outbound the muted tag
                        (money out) — the same pairing the status pills use for
                        done-vs-neutral, so the two reads don't compete. */}
                    {!isStatementLine && <span
                      className={`${styles.directionBadge} ${
                        p.direction === "INBOUND"
                          ? styles.directionIn
                          : styles.directionOut
                      }`}
                    >
                      {p.direction === "INBOUND" ? "IN" : "OUT"}
                    </span>}
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
                <td>
                  <div className={styles.statusCell}>
                    <StatusPill status={p.status} />
                    {/* A symbol, not a sentence: the full reason is the tooltip, and the
                        deep-dive carries the detail. */}
                    {p.exception && excOpen && (
                      <span
                        className={styles.exceptionIconOpen}
                        title={exceptionLine(p.exception)}
                        aria-label={exceptionLine(p.exception)}
                        role="img"
                      >
                        <Icon glyph="Warning" size={16} />
                      </span>
                    )}
                    {p.exception && excClosed && (
                      <span
                        className={styles.exceptionIconResolved}
                        title={resolvedLine(p.exception)}
                        aria-label={resolvedLine(p.exception)}
                        role="img"
                      >
                        <Icon glyph="Checkmark" size={16} />
                      </span>
                    )}
                  </div>
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
  refreshKey,
  onRefresh,
  selectedPaymentId,
  onSelect,
}) {
  const [filters, setFilters] = useState({
    status: "", rail: "", direction: "", corridor: "", customerId: "", from: "", to: "", skip: 0,
  });
  // Detailed filters are tucked away: search covers the common case.
  const [showFilters, setShowFilters] = useState(false);

  // The Activity list — every payment, newest first. Rows carry their joined exception
  // (open or latest resolved) so failed/returned payments show the reason + discrepancy
  // subline inline, the way the retired Operations queue did. The deep-dive owns the
  // resolve actions; this list is the scan surface.
  const { items, total, loading, error } = usePaymentsList(
    { ...filters, limit: PAGE_SIZE, skip: filters.skip },
    refreshKey
  );

  const move = (delta) =>
    setFilters((f) => ({ ...f, skip: Math.max(0, f.skip + delta * PAGE_SIZE) }));
  const clearFilters = () =>
    setFilters({ status: "", rail: "", direction: "", corridor: "", customerId: "", from: "", to: "", skip: 0 });
  const activeFilterCount = [filters.status, filters.rail, filters.direction, filters.corridor, filters.customerId, filters.from, filters.to]
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
          // B6: refresh the queue on Back so a resolved/closed exception's row is current,
          // not the pre-resolve snapshot the list hook is still holding. `onDataChanged`
          // also fires from inside the deep-dive right after a resolve/review/resume, so
          // the queue is fresh the moment the operator returns to it.
          onDataChanged={onRefresh}
          onBack={() => {
            if (onRefresh) onRefresh();
            onSelect(null);
          }}
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
              <Icon glyph="List" size={16} />
              <span className={styles.panelTitle}>Payments</span>
            </div>
            <span className={styles.panelDesc}>
              Payment orders initiated and moving through the lifecycle.
            </span>
          </div>
          <div className={styles.panelHeadRight}>
            <InboundTrigger onRefresh={onRefresh} onJump={onSelect} />
            <Button
              size="small"
              leftGlyph={<Icon glyph="Filter" />}
              aria-expanded={showFilters}
              onClick={() => setShowFilters((v) => !v)}
            >
              {activeFilterCount > 0 ? `Filters (${activeFilterCount})` : "Filters"}
            </Button>
            <Button size="small" leftGlyph={<Icon glyph="Refresh" />} onClick={onRefresh}>
              Refresh
            </Button>
          </div>
        </div>

        <div className={styles.panelBody}>
          <CommandSearch
            onJump={onSelect}
            onFilterCustomer={(v) =>
              setFilters((f) => ({ ...f, customerId: v, skip: 0 }))
            }
          />
          {showFilters && (
            <div className={styles.filterBar}>
              <Filters value={filters} onChange={setFilters} />
              {activeFilterCount > 0 && (
                <Button size="xsmall" onClick={clearFilters}>
                  Clear filters
                </Button>
              )}
            </div>
          )}
        </div>

        {error && (
          <div className={styles.panelBody}>
            <Banner variant="danger">Could not load payments — {error}</Banner>
          </div>
        )}

        {!error && loading && <div className={styles.emptyState}>Loading payments…</div>}

        {!error && !loading && items.length === 0 && (
          <div className={styles.emptyState}>No payments match these filters.</div>
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
