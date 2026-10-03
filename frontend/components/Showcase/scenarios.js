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
    title: "Fee deducted, sender pays",
    story: "The correspondent deducts a $25 fee from a wire where the sender agreed to bear all charges.",
    talkingPoint: "The agent proposes a $25 adjustment; code checks it matches the discrepancy exactly.",
    bank: "Barclays",
    amount: "4,850.00",
    expected: "Reconciled by adjustment",
    policyLine: "Discrepancy · cause FEE · bearer DEBT → only POST_ADJUSTMENT is allowed",
  },
  {
    key: "R1b",
    title: "Fee deducted, shared charges",
    story: "The same $25 deduction, but the charges are shared with the beneficiary.",
    talkingPoint: "Same $25, different books: the charge terms decide the action.",
    bank: "Barclays",
    amount: "3,275.00",
    expected: "Reconciled by accepting the discrepancy",
    policyLine: "Discrepancy · cause FEE · bearer SHAR → only ACCEPT_DISCREPANCY is allowed",
  },
  {
    key: "R2",
    title: "Altered reference",
    story: "The correspondent rewrites the payment reference, so the statement line matches nothing.",
    talkingPoint: "Fuzzy matching is the agent's job, never automatic. One link closes both exceptions.",
    bank: "Deutsche Bank",
    amount: "6,120.00",
    expected: "Reconciled by linking the statement line",
    policyLine: "Missing ↔ orphaned · cause REFERENCE_MISMATCH → LINK_STATEMENT_ENTRY, with approval",
  },
  {
    key: "R4",
    title: "Transposed amount",
    story: "The correspondent books 7,306.00 instead of 7,360.00.",
    talkingPoint: "A 54.00 gap is not a fee any correspondent charges, so the guard refuses FEE.",
    bank: "Royal Bank of Canada",
    amount: "7,360.00",
    expected: "Escalated to the correspondent",
    policyLine: "Discrepancy · FEE refused (54.00 is not a levied charge) → ESCALATE_TO_CORRESPONDENT",
  },
  {
    key: "R5",
    title: "Unknown statement line",
    story: "A clean wire, plus a line on the statement that belongs to no payment of ours.",
    talkingPoint: "Not every line on the statement belongs to us.",
    bank: "Barclays",
    amount: "5,630.00",
    expected: "Escalated to the correspondent",
    policyLine: "Orphaned · no candidate → ESCALATE_TO_CORRESPONDENT or DISMISS",
  },
  {
    key: "TL",
    title: "Late statement",
    story: "The wire is fine; the correspondent's statement just hasn't arrived yet.",
    talkingPoint: "RECHECK is the only action the agent may take on its own.",
    bank: "UBS",
    amount: "2,485.00",
    expected: "Reconciled once the statement arrives",
    policyLine: "Missing · cause TIMING → RECHECK runs autonomously; nothing else does",
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
    label: "Investigate",
    narration:
      "The Reconciliation Agent investigates: it reads the trace, the statement and the " +
      "precedents, records a cause, and proposes one action. Code checks the proposal " +
      "against policy before it can reach you.",
    gate: "proposal",
  },
  investigateTL: {
    key: "investigate",
    label: "Investigate",
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
    label: "Late statement",
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
