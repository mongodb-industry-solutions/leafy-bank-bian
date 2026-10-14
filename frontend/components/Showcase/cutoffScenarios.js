// Part D — the two cut-off scenarios and their step lists. The backend
// `cutoff_scenarios.SCENARIOS` is the source of truth for amounts, banks and start minutes;
// these cards only describe the story. The step machine runs in CutoffStepper.
// Keys stay in the URL only; never put them in visible copy.

// Who the presenter acts as. Raj is Maya's backup approver (backend `cutoff_seed`).
export const STAFF = {
  RAJ: "CUST-abc10004",
};

export const CUTOFF_SCENARIOS = [
  {
    key: "C1",
    closeOut: "watch",
    mode: "approval",
    modeLabel: "Rescued in time",
    icon: "Person",
    recommended: true,
    decision: {
      cause: { label: "Approver out of office", code: "APPROVAL" },
      constraint: { label: "Expediting needs approval", code: "GATED" },
      action: { label: "Expedite to Fedwire", code: "EXPEDITE" },
    },
    title: "Approver out of office",
    story:
      "A $25,000 domestic wire waits for a second signature, and the approver is out of office. " +
      "The agent reminds her, escalates to her backup, then asks to expedite once the internal cut-off passes.",
    bank: "JPMorgan Chase",
    // Mirrors `cutoff_scenarios._CREDITORS` and the scenario's purpose in the backend.
    wire: {
      beneficiary: "Midwest Steel Supply Co",
      bankName: "JPMorgan Chase",
      bic: "CHASUS33",
      country: "US",
      iban: "4410028871",
      purpose: "Steel coil order PO-55120",
    },
    beats: {
      start:
        "A $25,000 domestic wire to Midwest Steel Supply waits for a second signature at 17:05 ET. " +
        "Its approver, Maya, is out of office.",
      watch:
        "The agent reminds Maya, then escalates to her backup. Once the clock passes the internal " +
        "cut-off, only Fedwire's 18:45 cut-off is left.",
      unblock:
        "Raj, Maya's backup signatory, supplies the missing signature. The agent cannot approve its own request.",
      decide:
        "The agent asks to expedite the wire to Fedwire. Approving runs the expedite, and the wire must " +
        "still make the cut-off.",
      final: "The wire is submitted to Fedwire before its cut-off, and the agent re-reads the payment to confirm it.",
    },
    amount: "25,000.00",
    startEt: "17:05",
    expected: "Submitted to Fedwire before its cut-off",
    expectedOutcome: "SUBMITTED_IN_TIME",
    presenter: { clock: "18:16", clockLabel: "Move clock to 18:16" },
  },
  {
    key: "C3",
    closeOut: "fastForward",
    mode: "refuses",
    modeLabel: "Can't make it",
    icon: "Clock",
    recommended: true,
    decision: {
      cause: { label: "Account short of funds", code: "FUNDS" },
      constraint: { label: "No credit expected today", code: "WILL_MISS" },
      action: { label: "Hold for the next value date", code: "HOLD_NEXT_VALUE_DATE" },
    },
    title: "Short of funds",
    story:
      "A domestic wire is $3,200 short and no incoming credit will cover it today. " +
      "The agent tells the customer, then asks to hold the wire for the next value date.",
    bank: "Bank of America",
    wire: {
      beneficiary: "Lakeside Components LLC",
      bankName: "Bank of America",
      bic: "BOFAUS3N",
      country: "US",
      iban: "3310045592",
      purpose: "Component supply INV-3391",
    },
    beats: {
      start: "A domestic wire to Lakeside Components is $3,200 more than the account holds, at 16:45 ET.",
      watch:
        "The agent checks the account's funds, finds no incoming credit that covers the gap, and tells the " +
        "customer about the shortfall.",
      decide:
        "The agent asks to hold the wire for the next value date. Moving a wire to another day needs your approval.",
      final:
        "The wire is held for the next value date. Fast-forwarding moves the payment on to the next business " +
        "day so it can settle.",
    },
    amount: "balance + 3,200.00",
    startEt: "16:45",
    expected: "Held for the next value date",
    expectedOutcome: "HELD_NEXT_VALUE_DATE",
  },
];

export const CUTOFF_GROUPS = [
  {
    mode: "approval",
    icon: "Person",
    title: "Rescued in time",
    blurb: "The agent clears what it can on its own, then asks a human to expedite.",
  },
  {
    mode: "refuses",
    icon: "Clock",
    title: "Can't make it",
    blurb: "The agent says so early and asks to move the wire to another day.",
  },
];

// How the last act closes the payment. `watch`: it is already settling, so the panel only
// watches. `fastForward` adds one button that moves the payment on; the case keeps the
// agent's decision either way.
export const CLOSE_OUT = {
  fastForward: {
    button: "Fast-forward to next business day",
    copy:
      "The case keeps the agent's decision. Fast-forwarding moves the payment on to the next business day " +
      "so it can settle.",
    note: "Fast-forwarded to the next business day.",
  },
  watch: { button: null, copy: "The wire is already on its way. Watching it settle.", note: null },
};

export const closeOutFor = (key) => CLOSE_OUT[cutoffByKey(key)?.closeOut] || CLOSE_OUT.watch;

export const cutoffByKey = (key) => CUTOFF_SCENARIOS.find((s) => s.key === key) || null;

// Each step: `gate` (what must be true before its button), `run` (the presenter action its
// button performs), `title` and `summary` (the step card). `decide` and `final` steps have no
// button. What the step means for each payment is `beats[key]` on the scenario.
const STEP = {
  start: {
    key: "start", run: "start", button: "Send the wire", title: "A wire is held",
    summary: "A branch operator sends a domestic wire. Late in the business day it stops on a hold, before it reaches Fedwire.",
  },
  watchClock: {
    key: "watch", gate: "assessed", run: "clock", button: null, title: "The agent assesses, then the clock moves",
    summary:
      "The agent times the hold against the cut-off and acts on its own where policy allows. Then time passes " +
      "and the cut-off gets closer.",
  },
  approveRaj: {
    key: "unblock", run: "approveRaj", button: "Approve as Raj", title: "The blocker is cleared",
    summary: "The missing approval arrives from the backup signatory, so the hold can lift.",
  },
  watchProposal: {
    key: "watch", gate: "proposal", button: "Review the proposal", title: "The agent assesses the hold",
    summary:
      "The agent times the hold against the cut-off, acts on its own where policy allows, records its " +
      "assessment and proposes a resolution.",
  },
  decide: {
    key: "decide", gate: "proposal", decide: true, title: "You decide",
    summary: "The agent's proposal moves the wire, so it waits for a human. Code has already checked it against policy.",
  },
  // The closing panel's button (if the scenario has one) lives in the result, not the step bar.
  final: {
    key: "final", final: true, title: "Verifying the result",
    summary: "The agent re-reads the payment before it reports the result.",
  },
};

/** The ordered step list for a scenario. */
export function cutoffStepsFor(key) {
  const scenario = cutoffByKey(key);
  if (key === "C1") {
    const watch = { ...STEP.watchClock, button: scenario.presenter.clockLabel };
    return [STEP.start, watch, STEP.approveRaj, STEP.decide, STEP.final];
  }
  return [STEP.start, STEP.watchProposal, STEP.decide, STEP.final];
}
