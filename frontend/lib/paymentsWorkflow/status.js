/**
 * Payment status → Badge variant, shared by the customer transactions table and the
 * back-office workflow surface.
 *
 * Extracted from TransactionsTable so there is exactly one mapping. The lifecycle states
 * below (DRAFT … RECONCILED and the terminals) were added here — TransactionsTable never
 * renders them, so its output is unchanged; they previously fell to the darkgray default.
 *
 * Variants are LeafyGreen Badge's: green | blue | yellow | red | darkgray | lightgray.
 */
export const statusBadgeVariant = (status) => {
  switch ((status || "").toUpperCase()) {
    // Terminal success
    case "SETTLED":
    case "POSTED":
    case "COMPLETED":
    case "RECONCILED":
      return "green";
    // Moving through the saga
    case "PENDING":
    case "PROCESSING":
    case "IN_PROGRESS":
    case "SUBMITTED":
      return "yellow";
    // Failed or refused
    case "FAILED":
    case "REJECTED":
    case "CANCELLED":
    case "RETURNED":
    case "REVERSED":
      return "red";
    // Accepted but not yet executed
    case "INITIATED":
    case "VALIDATED":
    case "ENRICHED":
    case "FINAL_VALIDATED":
    case "ROUTED":
    case "AUTHORISED":
    case "APPROVED":
      return "blue";
    case "DRAFT":
      return "lightgray";
    default:
      return "darkgray";
  }
};

/** Check outcome → Badge variant. PASS/FAIL/SKIP is the stage-2 `checks[]` vocabulary. */
export const checkBadgeVariant = (outcome) => {
  switch ((outcome || "").toUpperCase()) {
    case "PASS":
      return "green";
    case "FAIL":
      return "red";
    case "SKIP":
      return "lightgray";
    default:
      return "darkgray";
  }
};

export const fmtAmount = (amount, currency = "USD") =>
  amount == null
    ? "—"
    : `${Number(amount).toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })} ${currency}`;

export const fmtWhen = (value) => {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString();
};
