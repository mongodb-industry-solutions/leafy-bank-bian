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
    beats: {
      initiate:
        "We send USD 4,850.00 to Northwind Traders at Barclays. The customer chose charge " +
        "bearer DEBT: they pay every fee, so the beneficiary must receive the full amount.",
      settle: "Barclays confirms settlement. So far, a perfectly normal wire.",
      glpost: "Our books say 4,850.00 left our nostro account.",
      statement:
        "Barclays's statement shows only 4,825.00. They took a 25.00 fee out of the payment, " +
        "which they shouldn't have, because our customer agreed to pay all charges.",
      match:
        "Books say 4,850.00; statement says 4,825.00. A 25.00 RECONCILIATION_DISCREPANCY " +
        "opens, and the agent is woken.",
      investigate:
        "Watch the agent find that 25.00 is exactly Barclays's published wire fee, classify " +
        "the cause as FEE, and see the bearer is DEBT. Under DEBT the fee is ours to absorb, " +
        "so policy allows exactly one action: post a 25.00 adjustment.",
      approve:
        "Approving posts a correcting journal: Dr 5214 correspondent charges / Cr 1111 " +
        "nostro, 25.00. Code rejects any adjustment that doesn't equal the discrepancy exactly.",
      verify:
        "The adjustment is in the general ledger, the books now agree with Barclays, and the " +
        "payment is RECONCILED. No analyst wrote the entry.",
    },
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
    beats: {
      initiate:
        "We send USD 3,275.00 to Barclays with charge bearer SHAR: charges are shared, so the " +
        "beneficiary may legitimately receive slightly less.",
      settle: "Barclays confirms settlement.",
      glpost: "Our books say 3,275.00 left our nostro account.",
      statement: "Barclays's statement shows 3,250.00: the same 25.00 fee as in R1.",
      match: "A 25.00 RECONCILIATION_DISCREPANCY opens, the same as in R1.",
      investigate:
        "The agent reaches the same cause, FEE, but this time the bearer is SHAR. The " +
        "beneficiary carries the fee, so there is nothing to book. Policy allows only " +
        "ACCEPT_DISCREPANCY. Same symptom as R1, different correct answer.",
      approve: "Approving accepts the discrepancy. No ledger entry is written, by design.",
      verify:
        "The exception is closed and the payment is RECONCILED, with no correcting journal. " +
        "The charge terms, not the agent's judgement, decided the books.",
    },
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
    beats: {
      initiate: "We send USD 6,120.00 to Fabrikam Logistics at Deutsche Bank.",
      settle: "Deutsche Bank confirms settlement.",
      glpost: "Our books say 6,120.00 left our nostro account.",
      statement:
        "The statement has a 6,120.00 line, but Deutsche Bank rewrote our reference into its " +
        "own format. The money is there; the label no longer matches.",
      match:
        "Matching is exact by design, so the line matches nothing and opens an " +
        "ORPHANED_SETTLEMENT exception: money on the statement we can't place.",
      overdue:
        "Our payment's own line hasn't been found either. Once the window passes, a MISSING " +
        "exception opens. Two exceptions now describe one problem from both sides.",
      investigate:
        "The agent searches for candidates: same amount, same correspondent, the reference " +
        "rearranged. It proposes linking the line to the payment. Fuzzy matching is a " +
        "proposal for a human to approve, never an automatic match.",
      approve: "Approving links the statement line to the payment, and one link closes both exceptions.",
      verify: "Both the MISSING and the ORPHANED exceptions are closed, and the payment is RECONCILED.",
    },
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
    beats: {
      initiate: "We send USD 7,360.00 to Adventure Works at Royal Bank of Canada.",
      settle: "RBC confirms settlement.",
      glpost: "Our books say 7,360.00 left our nostro account.",
      statement:
        "RBC's statement shows 7,306.00. Two digits are swapped: a keying error at the " +
        "correspondent, not a fee.",
      match: "A 54.00 RECONCILIATION_DISCREPANCY opens.",
      investigate:
        "A model could easily rationalise 54.00 as a fee plus an extra charge, and early " +
        "versions did. A guard in code now only accepts FEE as the cause when the gap equals " +
        "a charge the correspondent actually levies. RBC charges 25.00, so FEE is refused. " +
        "The agent classifies it as an AMOUNT_MISMATCH and proposes escalating to RBC.",
      approve:
        "Approving sends a camt.026 query to RBC. We don't write off or adjust money we " +
        "can't explain.",
      verify:
        "The exception stays open, marked as awaiting the correspondent. The case is now " +
        "RBC's to answer, and the books are untouched.",
    },
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
    beats: {
      initiate: "We send a routine USD 5,630.00 wire to Barclays. This payment is clean.",
      settle: "Barclays confirms settlement.",
      glpost: "Our books say 5,630.00 left our nostro account.",
      statement:
        "The statement carries our 5,630.00 line, plus one extra line we never sent: a " +
        "movement on our account with no payment behind it.",
      match:
        "Our wire matches and reconciles cleanly. The extra line matches nothing and opens " +
        "an ORPHANED_SETTLEMENT exception.",
      investigate:
        "The agent searches our payments for anything that could explain the line, and finds " +
        "no candidate. With nothing to link, policy allows only escalation or dismissal.",
      approve:
        "Approving sends a camt.026 query to Barclays to explain the line. We don't book " +
        "money we can't attribute.",
      verify: "The orphan is with Barclays, awaiting their answer, and our own wire is RECONCILED.",
    },
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
    beats: {
      initiate: "We send USD 2,485.00 to Tailspin Aviation at UBS.",
      settle: "UBS confirms settlement.",
      glpost: "Our books say 2,485.00 left our nostro account.",
      window:
        "UBS's statement is late, and that's all. Watch the countdown: when the window " +
        "closes, a MISSING exception opens for a payment that is actually fine.",
      investigate:
        "The agent sees a clean, settled payment that is merely overdue. It classifies the " +
        "cause as TIMING, rechecks, and schedules its next check, all without asking anyone, " +
        "because it moves no money. It can recheck up to 3 times over 10 minutes.",
      late:
        "UBS's statement arrives with our line on it. No one clicks anything; within a " +
        "minute the agent's sweep rechecks and finds the line.",
      verify:
        "The exception closes and the payment is RECONCILED. The agent handled a false " +
        "alarm on its own and never asked a human for anything.",
    },
  },
];

export const scenarioByKey = (key) => SCENARIOS.find((s) => s.key === key) || null;

// Seconds after settlement before the ledger calls a statement line overdue (MISSING).
export const OVERDUE_SECONDS = 60;

// Each step has a generic `narration` (what this step does, the same in every scenario) and
// each scenario has `beats[step.key]` (what it means for THIS payment). Read together, top to
// bottom, they are the presenter's script.
const STEP = {
  initiate: {
    key: "initiate",
    label: "Initiate",
    title: "A customer sends a wire",
    narration:
      "A branch operator sends an international wire for a customer. In one synchronous call " +
      "the payment passes the first five lifecycle stages: initiation, authentication and " +
      "entitlement, validation and enrichment, orchestration (fraud scoring and funds " +
      "reservation), and rail execution, where it leaves the bank as an ISO 20022 pacs.008. " +
      "Each stage is recorded on the payment document, and the customer's account is " +
      "debited in the same ACID transaction.",
    run: "initiate",
  },
  settle: {
    key: "settle",
    label: "Settle",
    title: "The correspondent confirms settlement",
    narration:
      "Our correspondent bank confirms it has settled the funds (stage 7). The payment flips " +
      "to SETTLED. Nothing calls the ledger directly: a MongoDB change stream sees the status " +
      "change and the ledger service writes its settlement event.",
    run: "settle",
  },
  glpost: {
    key: "glpost",
    label: "GL post",
    title: "The books are written",
    narration:
      "The general-ledger batch journals the payment's events into double-entry postings " +
      "(stage 6): customer account to clearing, clearing to our nostro account with the " +
      "correspondent (1111). Every journal balances to the cent. As far as our own books " +
      "are concerned, this payment is done.",
    run: "batch",
  },
  statement: {
    key: "statement",
    label: "Statement",
    title: "The correspondent's statement arrives",
    narration:
      "Our books only record what we believe happened. The correspondent sends its own " +
      "record: a camt.053 statement of every movement on our nostro account. This is the " +
      "external evidence reconciliation checks our books against.",
    run: "statement",
  },
  match: {
    key: "match",
    label: "Match",
    title: "Reconciliation compares the two",
    narration:
      "The next batch matches each statement line to a payment and runs a three-way " +
      "reconciliation: payment, ledger, statement. When all three agree, the payment is " +
      "RECONCILED with no human involved. When they don't, an exception opens, and the " +
      "change stream on the exceptions collection wakes the Reconciliation Agent.",
    run: "batch",
  },
  overdue: {
    key: "overdue",
    label: "Overdue",
    title: "The payment's own line is overdue",
    narration:
      "Every settled payment expects its line on a statement within a set window. When the " +
      "window passes with no matching line, the next batch raises a MISSING exception for " +
      "the payment.",
    run: "batch",
    countdown: true,
  },
  window: {
    key: "window",
    label: "Window",
    title: "The statement doesn't come",
    narration:
      "Every settled payment expects its line on a statement within a set window. When the " +
      "window passes with no matching line, the next batch raises a MISSING exception for " +
      "the payment.",
    run: "batch",
    countdown: true,
  },
  investigate: {
    key: "investigate",
    label: "Investigate",
    title: "The agent investigates",
    narration:
      "The agent works like an analyst: it reads the payment's full trace, the statement " +
      "line, the correspondent's charge policy and past precedents, records a cause with " +
      "its evidence, and proposes exactly one action. It can't act on that proposal itself. " +
      "Code checks the proposal against policy, which says which actions are allowed for " +
      "each cause, before a human ever sees it.",
    gate: "proposal",
  },
  investigateTL: {
    key: "investigate",
    label: "Investigate",
    title: "The agent investigates",
    narration:
      "The agent reads the trace and the statement history the way an analyst would, and " +
      "records a cause. Most actions need a human's approval. One doesn't: RECHECK moves no " +
      "money and changes no books, so policy lets the agent run it on its own.",
    gate: "recheck",
  },
  approve: {
    key: "approve",
    label: "Approve",
    title: "A human decides",
    narration:
      "Human in the loop. The proposal, its evidence and the policy rule it satisfies are " +
      "on screen. Approve executes it through the same service routes an operator would " +
      "use, so it may move money. Reject records the decision and ends the run. Either way, " +
      "the decision and who made it are stored on the exception.",
    gate: "decided",
  },
  late: {
    key: "late",
    label: "Late statement",
    title: "The statement finally arrives",
    narration:
      "The correspondent's statement turns up, late. Nobody touches the exception. The " +
      "agent's scheduled recheck sweep picks it up on its own.",
    run: "statementLate",
  },
  verify: {
    key: "verify",
    label: "Verify",
    title: "Check the result",
    narration:
      "The agent doesn't mark its own work as done. After acting, it re-reads the system and " +
      "only reports success once the exception has actually left the open queue. The " +
      "payment pane shows the final state: the exception, the reconciliation status, and " +
      "any correcting ledger entries.",
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
