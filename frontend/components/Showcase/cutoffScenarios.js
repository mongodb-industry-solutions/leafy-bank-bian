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

// The proposal each scenario waits for before the decision act.
const PROPOSAL = { C1: "EXPEDITE", C3: "HOLD_NEXT_VALUE_DATE" };
export const expectedProposal = (key) => PROPOSAL[key] || null;

// Each step: `act` (story bar), `gate` (what must be true before its button), `run` (the
// presenter action its button performs). `decide` and `final` steps have no button.
const STEP = {
  start: { key: "start", act: 1, run: "start", button: "Send the wire" },
  watchClock: { key: "watch", act: 2, gate: "assessed", run: "clock", button: null },
  approveRaj: { key: "unblock", act: 3, run: "approveRaj", button: "Approve as Raj" },
  watchProposal: { key: "watch", act: 2, gate: "proposal", button: "Review the proposal" },
  decide: { key: "decide", act: 4, gate: "proposal", decide: true },
  // The closing panel's button (if the scenario has one) lives in the scene, not the step bar.
  final: { key: "final", act: 5, final: true },
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

export const CUTOFF_ACTS = [
  { id: 1, label: "The wire is held", caption: "A wire is stuck on a hold late in the business day." },
  { id: 2, label: "Agent assesses", caption: "The agent times the hold against the cut-off and acts where it may." },
  { id: 3, label: "Clock moves", caption: "Time passes and the blocker is cleared." },
  { id: 4, label: "You decide", caption: "Approve or reject. Code has already checked the proposal against policy." },
  { id: 5, label: "Outcome", caption: "The agent re-reads the payment before reporting the result." },
];

/** The acts a scenario passes through. */
export function cutoffActsFor(key) {
  return key === "C1" ? CUTOFF_ACTS : CUTOFF_ACTS.filter((a) => a.id !== 3);
}

export const cutoffActOf = (step) => step?.act || 1;
