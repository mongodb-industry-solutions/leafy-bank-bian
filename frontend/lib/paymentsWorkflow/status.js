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
    // Held for manual review (FR-4.13) — needs an operator action, not yet authorised
    case "PENDING_REVIEW":
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

/** `checks[].result` → Badge variant. PASS/FAIL/SKIP/PENDING is the vocabulary in
 * `domain/checks.py`. PENDING belongs to an ASYNC check whose answer has not arrived —
 * unused by stage 2, whose six checks are all SYNC. */
export const checkBadgeVariant = (outcome) => {
  switch ((outcome || "").toUpperCase()) {
    case "PASS":
      return "green";
    case "FAIL":
      return "red";
    case "PENDING":
      return "yellow";
    case "SKIP":
      return "lightgray";
    default:
      return "darkgray";
  }
};

/**
 * Status → pill color family for the custom StatusPill (replaces @leafygreen-ui/badge on the
 * back-office analyst surface). Same status→color mapping as statusBadgeVariant, but returns
 * a family key the pill CSS classes resolve, instead of an LG Badge variant. TransactionsTable
 * keeps LG Badge, so statusBadgeVariant stays.
 *
 * Families map to LeafyGreen palette pairs (research §2 / §3.4): border + text = dark2 of the
 * family, background = light2. Pending/draft/unknown = gray outline.
 */
export const pillFamily = (status) => {
  switch ((status || "").toUpperCase()) {
    // Terminal success
    case "SETTLED":
    case "POSTED":
    case "COMPLETED":
    case "RECONCILED":
      return "green";
    // In flight
    case "PENDING":
    case "PROCESSING":
    case "IN_PROGRESS":
    case "SUBMITTED":
      return "yellow";
    // Held for manual review (FR-4.13) — needs an operator action, not yet authorised
    case "PENDING_REVIEW":
      return "yellow";
    // Failed or refused
    case "FAILED":
    case "REJECTED":
    case "CANCELLED":
    case "RETURNED":
    case "REVERSED":
    case "REFUNDED":
    case "DISCREPANT":
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
    case "UNRECONCILED":
      return "gray";
    default:
      return "gray";
  }
};

/** `checks[].result` → pill family. Same mapping as checkBadgeVariant; SKIP → gray. */
export const checkPillFamily = (outcome) => {
  switch ((outcome || "").toUpperCase()) {
    case "PASS":
      return "green";
    case "FAIL":
      return "red";
    case "PENDING":
      return "yellow";
    case "SKIP":
      return "gray";
    default:
      return "gray";
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
