// Part D — the five cut-off scenarios and their step lists. The backend
// `cutoff_scenarios.SCENARIOS` is the source of truth for amounts, banks and start minutes;
// these cards only describe the story. The step machine runs in CutoffStepper.
// Keys stay in the URL only; never put them in visible copy.

// Who the presenter acts as. Raj is Maya's backup approver; analyst 3 is the only
// screening analyst on shift after 17:00 (backend `cutoff_seed`).
export const STAFF = {
  RAJ: "CUST-abc10004",
  ANALYST: "STAFF-analyst-3",
};

export const CUTOFF_SCENARIOS = [
  {
    key: "C1",
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
    key: "C2",
    mode: "approval",
    modeLabel: "Rescued in time",
    icon: "Person",
    recommended: false,
    decision: {
      cause: { label: "Stuck in screening", code: "SCREENING" },
      constraint: { label: "Expediting needs approval", code: "GATED" },
      action: { label: "Expedite to Fedwire", code: "EXPEDITE" },
    },
    title: "Stuck in the screening queue",
    story:
      "An international wire sits eighth in the sanctions screening queue with one analyst left on shift. " +
      "The agent moves it to the front, then asks to expedite once it is cleared.",
    bank: "Emirates NBD",
    amount: "8,750.00",
    startEt: "17:50",
    expected: "Submitted to Fedwire before its cut-off",
    expectedOutcome: "SUBMITTED_IN_TIME",
    presenter: { clock: "18:16", clockLabel: "Move clock to 18:16" },
  },
  {
    key: "C3",
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
  {
    key: "C5",
    mode: "refuses",
    modeLabel: "Can't make it",
    icon: "Clock",
    recommended: false,
    decision: {
      cause: { label: "Past the internal cut-off", code: "CUTOFF" },
      constraint: { label: "Too late to expedite safely", code: "WILL_MISS" },
      action: { label: "Defer to the next business day", code: "DEFER_NEXT_BUSINESS_DAY" },
    },
    title: "Ten minutes to Fedwire",
    story:
      "A cross-border wire reaches the cut-off exception queue ten minutes before Fedwire closes. " +
      "The agent asks to defer it to the next business day instead of racing the clock.",
    bank: "Royal Bank of Canada",
    amount: "7,900.00",
    startEt: "18:35",
    expected: "Deferred to the next business day",
    expectedOutcome: "DEFERRED_NEXT_BUSINESS_DAY",
    presenter: { clock: "18:46", clockLabel: "Move clock past Fedwire (18:46)", optional: true },
  },
  {
    key: "C4",
    mode: "autonomous",
    modeLabel: "No action needed",
    icon: "Checkmark",
    recommended: false,
    decision: {
      cause: { label: "Second in screening", code: "SCREENING" },
      constraint: { label: "Well inside the cut-off", code: "ON_TRACK" },
      action: { label: "Do nothing", code: "NONE" },
    },
    title: "On track, leave it alone",
    story:
      "A wire sits second in the screening queue with hours to spare. " +
      "The agent checks the timing against history and decides not to act.",
    bank: "Barclays",
    amount: "6,400.00",
    startEt: "16:30",
    expected: "No action needed",
    expectedOutcome: null,
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
  {
    mode: "autonomous",
    icon: "Checkmark",
    title: "No action needed",
    blurb: "Knowing when not to act is part of the job.",
  },
];

export const cutoffByKey = (key) => CUTOFF_SCENARIOS.find((s) => s.key === key) || null;

// The proposal each scenario waits for before the decision act.
const PROPOSAL = { C1: "EXPEDITE", C2: "EXPEDITE", C3: "HOLD_NEXT_VALUE_DATE", C5: "DEFER_NEXT_BUSINESS_DAY" };
export const expectedProposal = (key) => PROPOSAL[key] || null;

// Each step: `act` (story bar), `gate` (what must be true before its button), `run` (the
// presenter action its button performs). `decide` and `final` steps have no button.
const STEP = {
  start: { key: "start", act: 1, run: "start", button: "Send the wire" },
  watchClock: { key: "watch", act: 2, gate: "assessed", run: "clock", button: null },
  approveRaj: { key: "unblock", act: 3, run: "approveRaj", button: "Approve as Raj" },
  clearScreening: { key: "unblock", act: 3, gate: "resolveBlocker", run: "clearScreening", button: "Clear screening as analyst" },
  watchProposal: { key: "watch", act: 2, gate: "proposal", button: "Review the proposal" },
  watchNone: { key: "watch", act: 2, gate: "none", button: "See the result" },
  decide: { key: "decide", act: 4, gate: "proposal", decide: true },
  final: { key: "final", act: 5, final: true },
};

/** The ordered step list for a scenario. */
export function cutoffStepsFor(key) {
  const scenario = cutoffByKey(key);
  if (key === "C1" || key === "C2") {
    const watch = { ...STEP.watchClock, button: scenario.presenter.clockLabel };
    const unblock = key === "C1" ? STEP.approveRaj : STEP.clearScreening;
    return [STEP.start, watch, unblock, STEP.decide, STEP.final];
  }
  if (key === "C4") return [STEP.start, STEP.watchNone, STEP.final];
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
  if (key === "C4") return CUTOFF_ACTS.filter((a) => a.id !== 3 && a.id !== 4);
  if (key === "C3" || key === "C5") return CUTOFF_ACTS.filter((a) => a.id !== 3);
  return CUTOFF_ACTS;
}

export const cutoffActOf = (step) => step?.act || 1;
