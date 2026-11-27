// The W1 walkthrough's run log: the same event model as runLog.js, derived from the race report and
// the polled payments. Events are listed in the order they should read, and keys are stable, so a
// re-render never adds a second line.

export const PAYMENTS = "The payments";
export const RACE = "The race";
export const SETTLEMENT = "Settlement and posting";
export const RECONCILIATION = "Reconciliation";

export const CONCURRENCY_FILTERS = [["all", "All"], ["code", "Code"], ["mongo", "MongoDB"]];

const ATTEMPT_DWELL_MS = 450;
const BEAT_DWELL_MS = 2600;

export const usd = (n) => `${n < 0 ? "-" : ""}USD ${Math.abs(n).toFixed(0)}`;

/** When this attempt's guarded reserve matched, so winners read in the order they won. */
export const reservedAt = (a) => a.ops.find((o) => o.command === "findAndModify" && o.collection === "accounts" && o.result === "MATCHED")?.atMs ?? Infinity;

/** One line per attempt in the order the guard decided, with the running balance the winners left. */
function attemptEvents(report) {
  const { openingBalance } = report.params;
  let available = openingBalance;
  const decided = [...report.attempts].sort((a, b) => reservedAt(a) - reservedAt(b) || a.worker - b.worker);
  return decided.map((a) => {
    const retries = a.writeConflicts > 0 ? `, retried after ${a.writeConflicts} write conflict${a.writeConflicts === 1 ? "" : "s"}` : "";
    if (a.outcome === "RESERVED") {
      const before = available;
      available -= a.amount;
      return {
        key: `g:${a.worker}`, phase: RACE, lane: "mongo", dwell: ATTEMPT_DWELL_MS,
        title: `Wire ${a.worker} reserved ${usd(a.amount)}`,
        detail: `available ${usd(before)} to ${usd(available)}${retries}`,
      };
    }
    return {
      key: `g:${a.worker}`, phase: RACE, lane: "mongo", dwell: ATTEMPT_DWELL_MS, tone: a.outcome === "ERROR" ? "alert" : undefined,
      title: `Wire ${a.worker} ${a.outcome === "ERROR" ? "failed" : "refused: not enough available"}`,
      detail: a.error || `available ${usd(available)} < ${usd(a.amount)}${retries}`,
    };
  });
}

function raceEvents(report) {
  const { workers, openingBalance } = report.params;
  const s = report.summary;
  const out = [
    {
      key: "g:guard", phase: RACE, lane: "mongo", tag: "Conditional update", dwell: BEAT_DWELL_MS,
      title: "The balance check is the update's filter on the shared account document",
      detail: JSON.stringify(report.guard.filter),
    },
    {
      key: "g:read", phase: RACE, lane: "code", dwell: BEAT_DWELL_MS,
      title: `All ${workers} wires read available = ${usd(openingBalance)}, then are released into the reserve together`,
      detail: "The guard does not trust this read. It only decides what the update's filter finds at write time.",
    },
    ...attemptEvents(report),
  ];
  if (s.totalWriteConflicts > 0) {
    out.push({
      key: "g:conflicts", phase: RACE, lane: "mongo", tone: "wait", tag: "Write conflict and retry", dwell: BEAT_DWELL_MS,
      title: `${s.totalWriteConflicts} write conflicts on the account document, all retried`,
      detail: "Writers that lost a race hit WriteConflict (112). with_transaction reran them on fresh state, where the filter saw the true balance.",
    });
  }
  out.push({
    key: "g:result", phase: RACE, lane: "code", card: true, tone: s.invariantHeld ? "good" : "alert", tag: "Live on MongoDB",
    title: `${s.invariantHeld ? "Invariant held" : "Invariant broken"}: available ${usd(s.finalAvailable)}, hold ${usd(s.finalHold)}`,
    items: [
      `${s.winners} reserved and debited (${report.attempts.filter((a) => a.outcome === "RESERVED").map((a) => usd(a.amount)).join(" + ")}), ${s.refused} refused${s.errors ? `, ${s.errors} errors` : ""}`,
      `${Math.round(s.elapsedMs / 1000)} s through the full saga`,
    ],
  });
  return out;
}

/** Lifecycle lines for the winners, from the payments the stepper polls. */
function lifecycleEvents(report, payments) {
  const out = [];
  report.attempts.filter((a) => a.outcome === "RESERVED").forEach((a) => {
    const lc = payments[a.paymentId]?.lifecycle || {};
    if (lc.settlementStatus === "SETTLED") {
      out.push({ key: `settled:${a.worker}`, phase: SETTLEMENT, lane: "code", dwell: ATTEMPT_DWELL_MS, title: `Wire ${a.worker} settled`, detail: "Stage 7: the correspondent confirmed the funds." });
    }
    if (lc.postingStatus === "POSTED") {
      out.push({
        key: `posted:${a.worker}`, phase: SETTLEMENT, lane: "mongo", tag: "Change stream", dwell: ATTEMPT_DWELL_MS,
        title: `Wire ${a.worker} journaled in the ledger`, detail: "Balanced double-entry postings, written from the settlement event.",
      });
    }
  });
  report.attempts.filter((a) => a.outcome === "RESERVED").forEach((a) => {
    if (payments[a.paymentId]?.lifecycle?.reconciliationStatus === "RECONCILED") {
      out.push({
        key: `reconciled:${a.worker}`, phase: RECONCILIATION, lane: "code", tone: "good", dwell: ATTEMPT_DWELL_MS,
        title: `Wire ${a.worker} reconciled`, detail: "Payment, ledger and statement agree to the cent.",
      });
    }
  });
  return out;
}

export function deriveConcurrencyEvents({ queued, live, payments, statement }) {
  const out = [];
  if (queued) {
    out.push(
      {
        key: "p:queue", phase: PAYMENTS, lane: "code", dwell: BEAT_DWELL_MS,
        title: "Wires of USD 80, 110 and 90 leave the same account",
        detail: "Stage 3, Validate & Enrich: each must reserve funds on the debtor account before it can be routed.",
      },
      {
        key: "p:account", phase: PAYMENTS, lane: "mongo", tag: "One shared document", dwell: BEAT_DWELL_MS,
        title: "The balance lives in a single account document",
        detail: "Every wire reserves against this one document, which is what lets MongoDB detect the collision.",
      }
    );
  }
  if (live) {
    out.push(...raceEvents(live), ...lifecycleEvents(live, payments));
    if (statement) {
      out.push({
        key: "statement", phase: RECONCILIATION, lane: "code", dwell: BEAT_DWELL_MS,
        title: `Statement ${statement.id} received`, detail: `${statement.lines} line${statement.lines === 1 ? "" : "s"} booked against our nostro.`,
      });
    }
  }
  return out;
}
