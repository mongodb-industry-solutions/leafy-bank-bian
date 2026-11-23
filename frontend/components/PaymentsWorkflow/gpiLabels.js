// Display labels for simulated SWIFT gpi tracking (backend: contexts/payment_rail/domain/gpi.py).
// Unknown codes fall back to the raw code via `labelOf`.

export const GPI_STATUS = {
  ACSP: "Accepted, passed on",
  ACCC: "Credited to beneficiary",
  RJCT: "Rejected",
};

export const GPI_REASON = {
  G000: "Forwarded to next gpi bank",
  G001: "Forwarded to non-gpi bank",
  AC04: "Account closed",
  FF01: "Invalid format (rail NACK)",
};

// NOT_TRACKED is deliberately absent: the pill is hidden for it.
export const TRACKING = {
  SENT: { label: "Sent", family: "blue" },
  IN_TRANSIT: { label: "In transit", family: "yellow" },
  CREDITED: { label: "Credited", family: "green" },
  REJECTED: { label: "Rejected", family: "red" },
  RETURNED: { label: "Returned", family: "red" },
};

// Mirrors the role constants in contexts/payment_orchestration/domain/routing.py.
export const HOP_ROLE = {
  DEBTOR_AGENT: "Debtor agent",
  SENDER_CORRESPONDENT: "Sender correspondent",
  INTERMEDIARY: "Intermediary",
  CREDITOR_AGENT: "Creditor agent",
};

export function labelOf(map, code) {
  if (code == null) return "—";
  return map[code] ?? code;
}

// A RJCT that ends a RETURNED story reads as a return, not a rejection.
export function gpiStatusLabel(event) {
  if (event?.gpiStatus === "RJCT" && event?.outcome === "RETURNED") return "Returned";
  return labelOf(GPI_STATUS, event?.gpiStatus);
}

export function elapsed(fromIso, toIso) {
  const from = Date.parse(fromIso);
  const to = Date.parse(toIso);
  if (Number.isNaN(from) || Number.isNaN(to)) return "—";
  const secs = Math.max(0, Math.round((to - from) / 1000));
  if (secs < 60) return `${secs} s`;
  return `${Math.floor(secs / 60)} min ${secs % 60} s`;
}
