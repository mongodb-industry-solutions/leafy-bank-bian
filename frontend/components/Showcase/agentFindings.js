// One-line "what the agent learned" for a tool call, read from the tool's result text. The steps
// endpoint cuts results at 600 characters, so every formatter tolerates truncated JSON and falls
// back to a pattern match, then to nothing. Tools not listed here (the cut-off agent's) show their
// first sentence when the result is prose and nothing when it is JSON.

const num = (v) => Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const label = (v) => String(v).replaceAll("_", " ").toLowerCase();
const first = (re, text) => (text.match(re) || [])[1];
const parse = (text) => {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
};
const clip = (text, n = 110) => (text.length > n ? `${text.slice(0, n - 1)}…` : text);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

const FINDINGS = {
  payment_trace_lookup(text) {
    const state = first(/"currentState":\s*"(\w+)"/, text);
    return state ? `Payment is ${state}` : null;
  },

  reconciliation_analysis(text) {
    const data = parse(text);
    if (data?.legs) {
      const off = data.legs.filter((l) => Number(l.delta));
      if (!off.length) return "Every leg agrees";
      const l = off[0];
      return `${label(l.leg)} leg: books ${num(l.left)} vs statement ${num(l.right)}, gap ${num(Math.abs(l.delta))}`;
    }
    const deltas = [...text.matchAll(/"delta":\s*(-?\d+(?:\.\d+)?)/g)].map((m) => Number(m[1])).filter(Boolean);
    return deltas.length ? `Gap of ${num(Math.abs(deltas[0]))} on a leg` : null;
  },

  find_statement_candidates(text) {
    if (/^"?No candidates/.test(text)) return "No candidate found";
    const n = (text.match(/"index":/g) || []).length;
    const score = first(/"score":\s*([\d.]+)/, text);
    return n ? `${plural(n, "candidate")}${score ? `, best score ${score}` : ""}` : null;
  },

  get_correspondent_profile(text) {
    if (/^No correspondent/.test(text)) return "No correspondent on this payment";
    const data = parse(text);
    if (!data) {
      const bank = first(/"bankName":\s*"([^"]+)"/, text);
      return bank ? `${bank} profile loaded` : null;
    }
    const charges = Object.entries(data.chargePolicy || {})
      .filter(([, v]) => typeof v === "number")
      .map(([k, v]) => `${k} ${num(v)}`);
    const p50 = data.bookingLag?.p50Seconds;
    return [
      data.bankName || data.bic,
      charges.length ? `charges ${charges.join(", ")}` : null,
      p50 != null ? `usually booked in ${Math.round(p50)}s` : null,
    ].filter(Boolean).join(" · ");
  },

  get_resolution_precedents(text) {
    if (/^No correspondent/.test(text)) return "No correspondent to compare with";
    const data = parse(text);
    const actions = Array.isArray(data) ? data.map((d) => d.action).filter(Boolean) : [...text.matchAll(/"action":\s*"(\w+)"/g)].map((m) => m[1]);
    if (Array.isArray(data) && !data.length) return "No similar cases yet";
    if (!actions.length) return null;
    const tally = actions.reduce((acc, a) => ({ ...acc, [a]: (acc[a] || 0) + 1 }), {});
    const [top, n] = Object.entries(tally).sort((a, b) => b[1] - a[1])[0];
    return `${plural(actions.length, "similar case")}, ${n} closed with ${label(top)}`;
  },

  record_investigation(text) {
    const m = text.match(/cause=(\w+), confidence=(\w+)\)\. Permitted actions: \[(.*)\]/);
    if (m) return `Cause ${m[1]}, ${m[2].toLowerCase()} confidence · allowed: ${m[3].replaceAll("'", "").replaceAll(",", ", ")}`;
    return /refused|must be|Not recorded/i.test(text) ? clip(text) : null;
  },

  propose_action(text) {
    const m = text.match(/^Proposed (\w+);/);
    return m ? `Proposed ${label(m[1])}, waiting for approval` : clip(text);
  },

  recheck_reconciliation: (text) => clip(text),

  schedule_recheck(text) {
    const m = text.match(/in (\d+) minute/);
    return m ? `Next check in ${m[1]} min` : clip(text);
  },
};

const generic = (text) => (/^[{[]/.test(text) ? null : clip(text.split(/(?<=[.!?])\s/)[0]));

/** The one-line finding for a finished tool call, or null when nothing useful can be said. */
export function findingFor(tool, result) {
  if (result == null) return null;
  const text = String(result).trim();
  if (!text) return null;
  try {
    return (FINDINGS[tool] || generic)(text) || null;
  } catch {
    return null;
  }
}
