// The eight payment lifecycle stages, shared by the pipeline strip and the reconciliation run
// log. Kept free of React and of the run-log modules so both can import it without a cycle.

export const STAGES = [
  { n: 1, label: "Initiation", what: "The wire request is recorded." },
  { n: 2, label: "Authentication & Authorization", what: "The caller is verified and entitlements are checked." },
  { n: 3, label: "Validate & Enrich", what: "The payment is validated, the beneficiary resolved and the funds reserved." },
  { n: 4, label: "Orchestration & Approval", what: "The wire is routed, fraud-scored and approved." },
  { n: 5, label: "Payment Execution", what: "A pacs.008 leaves the bank for the correspondent." },
  { n: 6, label: "Accounting & Posting", what: "The customer is debited in one ACID transaction and a ledger event is written." },
  { n: 7, label: "Clearing & Settlement", what: "The correspondent confirms it has settled the funds." },
  { n: 8, label: "Reconciliation", what: "Our books now wait to be matched against the correspondent's statement." },
];

// [stage the wire is in, held?]. The stage is where the wire is now, so every earlier stage is
// done. SETTLED sits at stage 8 because stage 7 is finished and reconciliation is next.
export const POSITION = {
  DRAFT: [1], INITIATED: [2], PENDING_APPROVAL: [2, true],
  VALIDATED: [3], ENRICHED: [3], PENDING_FUNDS: [3, true],
  FINAL_VALIDATED: [4], ROUTED: [4], AUTHORISED: [4], APPROVED: [4],
  PENDING_SCREENING: [4, true], CUTOFF_EXCEPTION: [4, true], MANUAL_FRAUD_REVIEW: [4, true],
  SUBMITTED: [5], IN_PROGRESS: [6], POSTED: [7], SETTLED: [8], RECONCILED: [8],
};

// The state each stage stands for when the strip shows a stage the live payment has moved past.
export const STAGE_STATE = [null, "DRAFT", "INITIATED", "VALIDATED", "AUTHORISED", "SUBMITTED", "IN_PROGRESS", "POSTED", "SETTLED"];

/** The furthest stage a lifecycle state has reached, 0 if it is unknown. */
export const stageOfState = (state) => (state === "COMPLETED" ? 8 : (POSITION[state] || [0])[0]);
