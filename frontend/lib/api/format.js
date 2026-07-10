/**
 * Shared formatting helpers for financial data display.
 * Used by composed hooks and page components.
 */

/**
 * Format a number as USD currency string.
 * @param {number} amount
 * @returns {string} e.g. "USD 1,234"
 */
export function formatCurrency(amount) {
  return `USD ${Math.abs(amount).toLocaleString()}`;
}

/**
 * Parse a date value into a Date. A bare calendar date ("YYYY-MM-DD", as stored
 * in bookingDate/valueDate) is parsed in the LOCAL zone, not UTC — otherwise
 * `new Date("2026-07-11")` is UTC midnight and buckets a day earlier for viewers
 * west of UTC (e.g. Toronto) vs. east (e.g. Mumbai). A booking date is a calendar
 * day, so it must render identically regardless of the viewer's timezone. Values
 * carrying a time component (createdAt, etc.) are genuine instants — parsed as-is.
 * @param {string|Date|number} value
 * @returns {Date}
 */
export function parseCalendarDate(value) {
  if (typeof value === "string") {
    const m = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  }
  return new Date(value);
}

/**
 * Format a date string to locale date.
 * @param {string} dateStr - ISO date string or similar
 * @returns {string} locale-formatted date, or empty string if invalid
 */
export function formatDate(dateStr) {
  if (!dateStr) return "";
  const d = parseCalendarDate(dateStr);
  return isNaN(d.getTime()) ? "" : d.toLocaleDateString();
}

/**
 * Map ISO 20022 Purpose codes to display categories.
 * Used to categorize external transactions for the transaction table.
 */
const PURPOSE_CODE_CATEGORIES = {
  GDDS: "Groceries",
  SVCS: "Utilities",
  TRPT: "Travel",
  SUBB: "Entertainment",
  OTHR: "Other",
  HLTH: "Healthcare",
  EDUC: "Other",
  RENT: "Utilities",
  INSUR: "Other",
};

/**
 * Map ISO 20022 BkTxCd subfamily to display categories.
 * Fallback when Purpose code is absent.
 */
const TX_CODE_CATEGORIES = {
  POSD: "Restaurants",
  OTHR: "Other",
  STDO: "Utilities",
  FEES: "Other",
};

/**
 * Derive a display category from an ISO 20022 transaction.
 * Works for both internal (Leafy Bank) and external bank transactions.
 * Priority: Purpose code → Transaction subfamily → "Other".
 */
export function txCategory(tx) {
  const purposeCode = tx.Purp?.Cd;
  if (purposeCode && PURPOSE_CODE_CATEGORIES[purposeCode]) {
    return PURPOSE_CODE_CATEGORIES[purposeCode];
  }
  const subFamily = tx.BkTxCd?.SubFmly;
  if (subFamily && TX_CODE_CATEGORIES[subFamily]) {
    return TX_CODE_CATEGORIES[subFamily];
  }
  return "Other";
}
