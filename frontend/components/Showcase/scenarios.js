// Plan E — the six reconciliation scenarios and their step lists. The backend
// `recon_scenarios.SCENARIOS` is the source of truth for amounts/banks; these cards
// only describe the story. The step machine itself runs in ScenarioStepper.

export const CATEGORY = {
  DISCREPANCY: "RECONCILIATION_DISCREPANCY",
  MISSING: "RECONCILIATION_MISSING",
  ORPHANED: "ORPHANED_SETTLEMENT",
};

export const SCENARIOS = [
  {
    key: "R1",
    title: "Fee, DEBT → adjustment",
    story: "The correspondent deducts $25 from a wire whose sender agreed to bear all charges.",
    bank: "Barclays",
    amount: "4,850.00",
    expected: "RECONCILED via adjustment",
    talkingPoint: "The agent proposes an adjustment and code checks it.",
    policyLine: "DISCREPANCY · cause FEE · bearer DEBT → only POST_ADJUSTMENT is allowed",
  },
  {
    key: "R1b",
    title: "Fee, SHAR → accept",
    story: "The same $25 deduction, but on shared charges.",
    bank: null,
    amount: "3,275.00",
    expected: "RECONCILED via accepted discrepancy",
    talkingPoint: "The same $25 is booked differently because the charge terms differ.",
    policyLine: "DISCREPANCY · cause FEE · bearer SHAR → only ACCEPT_DISCREPANCY is allowed",
  },
  {
    key: "R2",
    title: "Altered reference → link",
    story: "The statement line carries a mangled reference, so nothing matches it automatically.",
    bank: "Deutsche Bank",
    amount: "6,120.00",
    expected: "RECONCILED via link (closes both twins)",
    talkingPoint: "Fuzzy matching is the agent's job, never automatic.",
    policyLine: "MISSING ↔ ORPHANED · cause REFERENCE_MISMATCH → LINK_STATEMENT_ENTRY, with approval",
  },
  {
    key: "R4",
    title: "Transposed amount → escalate",
    story: "The correspondent books 7,306 instead of 7,360.",
    bank: "Royal Bank of Canada",
    amount: "7,360.00 → 7,306.00",
    expected: "ESCALATED (camt.026 to the correspondent)",
    talkingPoint: "The guard refuses FEE because 54 is not a levied charge.",
    policyLine: "DISCREPANCY · FEE refused (54 is not a levied charge) → ESCALATE_TO_CORRESPONDENT",
  },
  {
    key: "R5",
    title: "Orphan line → escalate",
    story: "A clean wire, plus an unknown line injected into the statement.",
    bank: null,
    amount: "clean wire + unknown line",
    expected: "ESCALATED (camt.026 for the orphan)",
    talkingPoint: "Not every line on the statement belongs to us.",
    policyLine: "ORPHANED · no candidate → ESCALATE_TO_CORRESPONDENT or DISMISS",
  },
  {
    key: "TL",
    title: "Timing lag → autonomous recheck",
    story: "The statement is late. The wire is fine; the evidence just hasn't arrived.",
    bank: "UBS",
    amount: "fresh amount (< 10k)",
    expected: "RECONCILED after the late statement",
    talkingPoint: "RECHECK is the only action the agent may take on its own.",
    policyLine: "MISSING · cause TIMING → RECHECK runs autonomously; nothing else does",
  },
];

export const scenarioByKey = (key) => SCENARIOS.find((s) => s.key === key) || null;

// Seconds after settlement before the ledger calls a statement line overdue (MISSING).
export const OVERDUE_SECONDS = 60;

const STEP = {
  initiate: {
    key: "initiate",
    label: "Initiate",
    narration:
      "Initiate one wire for this scenario. It runs stages 1–5 synchronously — validation, " +
      "enrichment, fraud, routing — and lands on the rail as a pacs.008.",
    run: "initiate",
  },
  settle: {
    key: "settle",
    label: "Settle",
    narration:
      "Confirm external settlement (stage 7). The settlement status flips to SETTLED, and the " +
      "settlement ledger event follows via a change stream.",
    run: "settle",
  },
  glpost: {
    key: "glpost",
    label: "GL post",
    narration:
      "Run the GL batch. The principal and settlement events journal into the general ledger " +
      "(stage 6), balanced to the minor unit.",
    run: "batch",
  },
  statement: {
    key: "statement",
    label: "Statement",
    narration:
      "The correspondent's camt.053 statement arrives for the nostro (1111). This is the " +
      "external evidence reconciliation checks the books against.",
    run: "statement",
  },
  match: {
    key: "match",
    label: "Match",
    narration:
      "Run the batch again: the statement lines are matched and the three-way reconciliation " +
      "runs. Any break opens an exception, and the change stream wakes the agent.",
    run: "batch",
  },
  overdue: {
    key: "overdue",
    label: "Overdue",
    narration:
      "The altered line could not be matched, so the payment's own line is still missing. " +
      "Once the window passes, the next batch raises the MISSING twin.",
    run: "batch",
    countdown: true,
  },
  window: {
    key: "window",
    label: "Window",
    narration:
      "No statement has arrived. Once the expected window passes, the next batch flags the " +
      "payment's statement line as MISSING.",
    run: "batch",
    countdown: true,
  },
  investigate: {
    key: "investigate",
    label: "Agent",
    narration:
      "The Reconciliation Agent investigates: it reads the trace, the statement and the " +
      "precedents, records a cause, and proposes one action. Code checks the proposal " +
      "against policy before it can reach you.",
    gate: "proposal",
  },
  investigateTL: {
    key: "investigate",
    label: "Agent",
    narration:
      "The agent investigates, sees the correspondent is simply late, and acts on its own: " +
      "it rechecks and schedules the next check. RECHECK moves no money, so it needs no approval.",
    gate: "recheck",
  },
  approve: {
    key: "approve",
    label: "Approve",
    narration:
      "Human in the loop. Approve executes the proposal through the same routes an operator " +
      "uses — it may move money. Reject records the decision and ends the run.",
    gate: "decided",
  },
  late: {
    key: "late",
    label: "Late stmt",
    narration:
      "The statement finally arrives. Nobody touches the exception: the agent's scheduled " +
      "sweep picks it up, rechecks and closes it.",
    run: "statementLate",
  },
  verify: {
    key: "verify",
    label: "Verify",
    narration:
      "Verify the outcome against the books: the exception's final state, the payment's " +
      "reconciliation status, and any correcting GL legs.",
    final: true,
  },
};

/** The ordered step list for a scenario. */
export function stepsFor(key) {
  const head = [STEP.initiate, STEP.settle, STEP.glpost];
  if (key === "TL") {
    return [...head, STEP.window, STEP.investigateTL, STEP.late, STEP.verify];
  }
  const middle = key === "R2" ? [STEP.statement, STEP.match, STEP.overdue] : [STEP.statement, STEP.match];
  return [...head, ...middle, STEP.investigate, STEP.approve, STEP.verify];
}
