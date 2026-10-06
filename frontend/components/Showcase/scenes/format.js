const FMT = { minimumFractionDigits: 2, maximumFractionDigits: 2 };

/** Minor units (cents) to "1,234.56"; "—" when absent. */
export const minor = (v) => (v == null ? "—" : (Math.abs(Number(v)) / 100).toLocaleString(undefined, FMT));

/** Major units to "1,234.56". */
export const major = (v) => (v == null ? "—" : Number(v).toLocaleString(undefined, FMT));

/** The rail-settlement leg: our books (left) against the correspondent statement (right). */
export const railLegOf = (trace) => (trace?.reconciliation?.legs || []).find((l) => l.leg === "RAIL_SETTLEMENT");

/** Absolute gap in minor units, or null when there is no statement figure yet. */
export const gapOf = (leg) =>
  leg && leg.rightAmount != null ? Math.abs(Number(leg.leftAmount) - Number(leg.rightAmount)) : null;
