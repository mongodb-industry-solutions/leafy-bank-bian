import React, { useState } from "react";
import Icon from "@leafygreen-ui/icon";
import IconButton from "@leafygreen-ui/icon-button";
import Code from "@leafygreen-ui/code";
import Badge from "@leafygreen-ui/badge";
import styles from "./TransactionsTable.module.css";

// Leafy Bank transactions get a green badge; any other (external) bank gets blue.
const bankBadgeVariant = (bank) =>
  (bank || "").toLowerCase().replace(/\s/g, "") === "leafybank" ? "green" : "blue";

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
  Utilities: "#EF4444",
  "Clothing Stores": "#EC4899",
  "Department Stores": "#EC4899",
  Healthcare: "#06B6D4",
  Pharmacy: "#06B6D4",
  Other: "#dfdfdf",
};

const defaultGetCategoryColor = (category) => {
  return categoryColors[category] || categoryColors.Other;
};

export default function TransactionsTable({
  transactions = [],
  loading = false,
  getCategoryColor,
  includeExpand = true,
}) {
  const [expandedRow, setExpandedRow] = useState(null);

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
            <th className={styles.th} style={{ textAlign: "right" }}>
              Amount
            </th>
            {includeExpand && <th className={styles.th}></th>}
          </tr>
        </thead>
        <tbody>
          {loading ? (
            <tr>
              <td colSpan={includeExpand ? 5 : 4}>Loading transactions...</td>
            </tr>
          ) : transactions.length > 0 ? (
            (() => {
              // group transactions by local date (YYYY-MM-DD)
              const groups = {};
              const groupLabels = {};

              transactions.forEach((t) => {
                const raw = t._rawDate || t.date || "";
                let dateValue = new Date(raw);
                if (isNaN(dateValue)) {
                  dateValue = new Date(t.date || raw);
                }

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
                  if (key === "unknown") {
                    groupLabels[key] = t.date || "Unknown";
                  }
                }
                groups[key].push(t);
              });

              const sortedKeys = Object.keys(groups).sort((a, b) => {
                if (a === "unknown") return 1;
                if (b === "unknown") return -1;
                return b.localeCompare(a);
              });
              const today = new Date();
              const todayKey = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(
                today.getDate()
              ).padStart(2, "0")}`;

              return sortedKeys.map((key) => {
                const items = groups[key];
                const label = key === "unknown"
                  ? groupLabels[key] || "Unknown"
                  : key === todayKey
                  ? "Today"
                  : new Date(key).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

                return (
                  <React.Fragment key={key}>
                    <tr className={styles.dayHeader}>
                      <td colSpan={includeExpand ? 5 : 4} className={styles.dayHeaderCell}>
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
                              <IconButton
                                aria-label="expand-row"
                                className={styles.iconButton}
                                onClick={() => setExpandedRow(expandedRow === `${key}-${idx}` ? null : `${key}-${idx}`)}
                              >
                                <Icon glyph="CurlyBraces" />
                              </IconButton>
                            </td>
                          )}
                        </tr>

                        {includeExpand && expandedRow === `${key}-${idx}` && (
                          <tr className={styles.expandedRow}>
                            <td colSpan={includeExpand ? 5 : 4}>
                              <div className={styles.expandedContent}>
                                <Code language="json">{JSON.stringify(t._rawDocument || t, null, 2)}</Code>
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
              <td colSpan={includeExpand ? 5 : 4}>No transactions found</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
