// GL monitor formatting helpers — ported from .docs/gl-monitor-static-ref/js/utils.js.

export function fmt(amount, minor = false) {
  if (amount == null) return "—";
  const v = minor ? (amount / 100).toFixed(2) : amount.toFixed ? amount.toFixed(2) : amount;
  return Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function fmtMinor(v) {
  return v != null ? v.toLocaleString() + " minor" : "—";
}

// BSON dates serialize without timezone offset; treat them as UTC to avoid skew.
export function parseTs(v) {
  if (!v) return new Date(NaN);
  if (typeof v === "string" && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(v)) return new Date(v + "Z");
  return new Date(v);
}

export function fmtDate(s) {
  if (!s) return "—";
  try {
    return parseTs(s).toLocaleString("en-US", {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch {
    return s;
  }
}

// Maps a status to its chip CSS variant key (e.g. "pending", "posted", "dr").
export function chipVariant(status) {
  const s = (status || "").toUpperCase();
  const map = {
    PENDING: "pending", POSTED: "posted", FAILED: "failed",
    SETTLED: "settled", COMPLETED: "settled", DEBIT: "dr", CREDIT: "cr",
  };
  return map[s] || "neutral";
}

// Builds the query string appended to a /pipeline/* path.
export function buildParams(period, extra = {}) {
  const p = new URLSearchParams();
  const trimmed = (period || "").trim();
  if (trimmed) p.set("periodCode", trimmed);
  for (const [k, v] of Object.entries(extra)) if (v) p.set(k, v);
  return p.toString() ? "?" + p.toString() : "";
}
