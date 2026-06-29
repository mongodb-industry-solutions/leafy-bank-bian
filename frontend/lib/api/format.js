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
 * Format a date string to locale date.
 * @param {string} dateStr - ISO date string or similar
 * @returns {string} locale-formatted date, or empty string if invalid
 */
export function formatDate(dateStr) {
  if (!dateStr) return "";
  const d = new Date(dateStr);
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
