import React, { useState } from "react";
import Icon from "@leafygreen-ui/icon";
import IconButton from "@leafygreen-ui/icon-button";
import Code from "@leafygreen-ui/code";
import Badge from "@leafygreen-ui/badge";
import Tooltip from "@leafygreen-ui/tooltip";
import styles from "./TransactionsTable.module.css";
import { parseCalendarDate } from "@/lib/api/format";

// Leafy Bank transactions get a green badge; any other (external) bank gets blue.
const bankBadgeVariant = (bank) =>
  (bank || "").toLowerCase().replace(/\s/g, "") === "leafybank" ? "green" : "blue";

const statusBadgeVariant = (status) => {
  switch ((status || "").toUpperCase()) {
    case "SETTLED":
    case "POSTED":
    case "COMPLETED": return "green";
    case "PENDING":
    case "PROCESSING": return "yellow";
    case "FAILED":
    case "REJECTED":
    case "CANCELLED": return "red";
    default: return "darkgray";
  }
};

// Internal transactions belong to Leafy Bank; external ones carry their source.
const bankFor = (t) =>
  t.bank || (t._isExternal ? t._sourceInstitution || "External" : "Leafy Bank");

const categoryColors = {
  Groceries: "#10B981",
  Restaurants: "#F59E0B",
  Travel: "#3B82F6",
  Entertainment: "#8B5CF6",
  "Movie Theatres": "#8B5CF6",
  "Streaming Services": "#8B5CF6",
  "Clothing Stores": "#EC4899",
  "Department Stores": "#EC4899",
  Pharmacy: "#06B6D4",
  // External open-finance feed categories (transactionCategory field).
  // Colors match the "Spending per Category" Atlas Chart legend.
  Shopping: "#00C767",
  Services: "#1A6DFF",
  SavingsTransfer: "#F0AD00",
  Transport: "#E6007E",
  Healthcare: "#00B3E6",
  Utilities: "#12594B",
  Insurance: "#F06E0A",
  Medical: "#9B5DE5",
  Subscription: "#0D7C86",
  AccountTransfer: "#5C6C75",
  Rent: "#12594B",
  Other: "#B31B1B",
};

const defaultGetCategoryColor = (category) =>
  categoryColors[category] || categoryColors.Other;

const TIMELINE_STEPS = {
  SETTLED:    ["Initiated", "Processing", "Settled"],
  COMPLETED:  ["Initiated", "Processing", "Completed"],
  POSTED:     ["Initiated", "Processing", "Posted"],
  PENDING:    ["Initiated", "Processing"],
  PROCESSING: ["Initiated", "Processing"],
  FAILED:     ["Initiated", "Processing", "Failed"],
  REJECTED:   ["Initiated", "Rejected"],
  CANCELLED:  ["Initiated", "Cancelled"],
};

const timelineStepColor = (step) => {
  if (["Failed", "Rejected", "Cancelled"].includes(step)) return "#EF4444";
  if (["Settled", "Completed", "Posted"].includes(step)) return "#10B981";
  return "#6366f1";
};

function TransactionDetail({ t }) {
  const status = (t.transactionStatus || t.status || "").toUpperCase();
  const steps = TIMELINE_STEPS[status] || ["Initiated"];
  const txId = t.paymentId || t.transactionId || t.id || null;

  // Build per-step timestamps from available fields on the raw document.
  const raw = t._rawDocument || t;
  // Prefer the full ISO instant (createdAt) so Date & Time matches the timeline's
  // "Initiated" value; _rawDate/date are date-only strings that new Date() parses
  // as UTC midnight and misrender a day early west of UTC.
  const rawDate = raw.createdAt || t.createdAt || t._rawDate || t.date || null;
  const fmtTs = (val) => {
    if (!val) return null;
    const d = new Date(val);
    return isNaN(d) ? null : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };
  const initiatedAt = fmtTs(raw.createdAt);
  const finalAt = fmtTs(raw.clearing?.settledAt || raw.updatedAt);
  const stepTimestamps = steps.reduce((acc, step, i) => {
    if (i === 0) acc[step] = initiatedAt;
    else if (i === steps.length - 1) acc[step] = finalAt;
    return acc;
  }, {});
  const dateObj = rawDate ? new Date(rawDate) : null;
  const fmtDateTime = dateObj && !isNaN(dateObj)
    ? dateObj.toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : "—";
const amountFmt = typeof t.amount === "number"
    ? t.amount.toLocaleString(undefined, { style: "currency", currency: t.currency || "USD" })
    : t.amount || "—";

  return (
    <div className={styles.detailPanel}>
      <div className={styles.detailGrid}>

        {/* section 1: transaction details */}
        <div className={styles.detailSection}>
          <div className={styles.detailSectionTitle}>Transaction Details</div>
          <table className={styles.kvTable}>
            <tbody>
              {txId && (
                <tr className={styles.kvRow}>
                  <td className={styles.kvLabel}>Payment ID</td>
                  <td className={styles.kvValue}><span className={styles.detailMono}>{txId}</span></td>
                </tr>
              )}
              <tr className={styles.kvRow}>
                <td className={styles.kvLabel}>Amount</td>
                <td className={styles.kvValue}><strong>{amountFmt}</strong></td>
              </tr>
              <tr className={styles.kvRow}>
                <td className={styles.kvLabel}>Status</td>
                <td className={styles.kvValue}><Badge variant={statusBadgeVariant(status)}>{status || "—"}</Badge></td>
              </tr>
              {t.rail && (
                <tr className={styles.kvRow}>
                  <td className={styles.kvLabel}>Rail</td>
                  <td className={styles.kvValue}><span className={styles.detailMono}>{t.rail}</span></td>
                </tr>
              )}
              {(t.paymentType || t.type) && (
                <tr className={styles.kvRow}>
                  <td className={styles.kvLabel}>Type</td>
                  <td className={styles.kvValue}><span className={styles.detailMono}>{t.paymentType || t.type}</span></td>
                </tr>
              )}
              <tr className={styles.kvRow}>
                <td className={styles.kvLabel}>Date & Time</td>
                <td className={styles.kvValue}>{fmtDateTime}</td>
              </tr>
              {t.customerId && (
                <tr className={styles.kvRow}>
                  <td className={styles.kvLabel}>Customer</td>
                  <td className={styles.kvValue}><span className={styles.detailMono}>{t.customerId}</span></td>
                </tr>
              )}
              {t.remittance?.unstructured && (
                <tr className={styles.kvRow}>
                  <td className={styles.kvLabel}>Remittance</td>
                  <td className={styles.kvValue}>{t.remittance.unstructured}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

{/* section 3: timeline */}
        <div className={styles.detailSection}>
          <div className={styles.detailSectionTitle}>Timeline</div>
          <div className={styles.timelineTrack}>
            {steps.map((step, i) => {
              const color = timelineStepColor(step);
              const isLast = i === steps.length - 1;
              const ts = stepTimestamps[step];
              return (
                <div key={step} className={styles.timelineStep}>
                  <div className={styles.timelineLeft}>
                    <div className={styles.timelineDot} style={{ background: color, boxShadow: `0 0 0 3px ${color}22` }} />
                    {!isLast && <div className={styles.timelineLine} />}
                  </div>
                  <div className={styles.timelineStepBody}>
                    <span className={styles.timelineLabel} style={{ color: isLast ? color : "#374151" }}>{step}</span>
                    {ts && <span className={styles.timelineTs}>{ts}</span>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

      </div>
    </div>
  );
}

export default function TransactionsTable({
  transactions = [],
  loading = false,
  getCategoryColor,
  includeExpand = true,
}) {
  const [expandedRow, setExpandedRow] = useState(null);
  const [detailRow, setDetailRow] = useState(null);

  const colorFor = (category) => {
    if (typeof getCategoryColor === "function") return getCategoryColor(category);
    return defaultGetCategoryColor(category);
  };

  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th className={styles.th}></th>
            <th className={styles.th}>Transaction</th>
            <th className={styles.th}>Institution</th>
            <th className={styles.th}>Status</th>
            <th className={styles.th} style={{ textAlign: "right" }}>Amount</th>
            {includeExpand && <th className={styles.th}></th>}
          </tr>
        </thead>
        <tbody>
          {loading ? (
            <tr>
              <td colSpan={includeExpand ? 6 : 5}>Loading transactions...</td>
            </tr>
          ) : transactions.length > 0 ? (
            (() => {
              const groups = {};
              const groupLabels = {};

              transactions.forEach((t) => {
                const raw = t._rawDate || t.date || "";
                let dateValue = parseCalendarDate(raw);
                if (isNaN(dateValue)) dateValue = parseCalendarDate(t.date || raw);

                let key;
                if (isNaN(dateValue)) {
                  key = "unknown";
                } else {
                  const y = dateValue.getFullYear();
                  const m = String(dateValue.getMonth() + 1).padStart(2, "0");
                  const day = String(dateValue.getDate()).padStart(2, "0");
                  key = `${y}-${m}-${day}`;
                }

                if (!groups[key]) {
                  groups[key] = [];
                  if (key === "unknown") groupLabels[key] = t.date || "Unknown";
                }
                groups[key].push(t);
              });

              const sortedKeys = Object.keys(groups).sort((a, b) => {
                if (a === "unknown") return 1;
                if (b === "unknown") return -1;
                return b.localeCompare(a);
              });
              const today = new Date();
              const todayKey = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;

              return sortedKeys.map((key) => {
                const items = groups[key];
                const label = key === "unknown"
                  ? groupLabels[key] || "Unknown"
                  : key === todayKey
                  ? "Today"
                  : parseCalendarDate(key).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

                return (
                  <React.Fragment key={key}>
                    <tr className={styles.dayHeader}>
                      <td colSpan={includeExpand ? 6 : 5} className={styles.dayHeaderCell}>
                        <div className={styles.dayHeaderLabel}>{label}</div>
                      </td>
                    </tr>
                    {items.map((t, idx) => (
                      <React.Fragment key={idx}>
                        <tr>
                          <td className={styles.categoryCircleCell}>
                            <div
                              className={styles.categoryCircle}
                              style={{ backgroundColor: colorFor(t.category) }}
                            />
                          </td>
                          <td>
                            <div className={styles.transactionDetails}>
                              <div className={styles.establishment}>{t.establishment}</div>
                              <div className={styles.category}>{t.category}</div>
                            </div>
                          </td>
                          <td>
                            {bankFor(t) && (
                              <Badge variant={bankBadgeVariant(bankFor(t))}>
                                {bankFor(t)}
                              </Badge>
                            )}
                          </td>
                          <td>
                            {(t.transactionStatus || t.status) && (
                              <Badge variant={statusBadgeVariant(t.transactionStatus || t.status)}>
                                {(t.transactionStatus || t.status).toUpperCase()}
                              </Badge>
                            )}
                          </td>
                          <td style={{ textAlign: "right" }}>
                            <strong>
                              {t.amount.toLocaleString(undefined, {
                                style: "currency",
                                currency: "USD",
                              })}
                            </strong>
                          </td>
                          {includeExpand && (
                            <td>
                              <div className={styles.actionBtns}>
                                <Tooltip trigger={
                                  <IconButton
                                    aria-label="view-details"
                                    className={styles.iconButton}
                                    onClick={() => {
                                      const id = `${key}-${idx}`;
                                      setDetailRow(detailRow === id ? null : id);
                                      if (expandedRow === id) setExpandedRow(null);
                                    }}
                                  >
                                    <Icon glyph="InfoWithCircle" />
                                  </IconButton>
                                }>Transaction details &amp; Timeline</Tooltip>
                                <IconButton
                                  aria-label="expand-row"
                                  className={styles.iconButton}
                                  onClick={() => {
                                    const id = `${key}-${idx}`;
                                    setExpandedRow(expandedRow === id ? null : id);
                                    if (detailRow === id) setDetailRow(null);
                                  }}
                                >
                                  <Icon glyph="CurlyBraces" />
                                </IconButton>
                              </div>
                            </td>
                          )}
                        </tr>

                        {includeExpand && detailRow === `${key}-${idx}` && (
                          <tr className={styles.expandedRow}>
                            <td colSpan={6}>
                              <TransactionDetail t={t} />
                            </td>
                          </tr>
                        )}

                        {includeExpand && expandedRow === `${key}-${idx}` && (
                          <tr className={styles.expandedRow}>
                            <td colSpan={6}>
                              <div className={styles.expandedContent}>
                                <div className={styles.jsonWrap}>
                                  <Code language="json" copyButtonAppearance="none">{JSON.stringify(t._rawDocument || t, null, 2)}</Code>
                                </div>
                              </div>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    ))}
                  </React.Fragment>
                );
              });
            })()
          ) : (
            <tr>
              <td colSpan={includeExpand ? 6 : 5}>No transactions found</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
