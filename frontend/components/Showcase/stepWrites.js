// What each walkthrough step writes to MongoDB: collection, operation, the fields it sets,
// and a picker returning the LIVE document(s) from the sources the stepper already polls.
// Live, not snapshotted: a later step may have changed the doc since, which is why `fields`
// names what THIS step wrote. Writers verified against the services' code (2026-10-03).
//
// sources = { payment, trace, ownException, orphan, agent, followedException, scenarioKey }
//   payment — GET /workflow/payments/{id}: the payments doc plus joined collections
//   trace   — GET /pipeline/trace/{id}: the ledger's documents for the payment

// get_payment joins these onto the payments doc; strip them to show the doc as stored.
const JOINED = ["executions", "messages", "settlementPositions", "routingSnapshot", "exceptions", "statements"];

export function paymentDoc(payment) {
  if (!payment) return null;
  const doc = { ...payment };
  JOINED.forEach((k) => delete doc[k]);
  return doc;
}

export const nonEmpty = (docs) => {
  const list = (Array.isArray(docs) ? docs : [docs]).filter(Boolean);
  return list.length ? list : null;
};

export const pacsMessages = ({ payment }) =>
  nonEmpty((payment?.messages || []).filter((m) => !m.purpose));
export const position = ({ payment }) => nonEmpty((payment?.settlementPositions || []).slice(-1));
export const statements = ({ payment }) => nonEmpty(payment?.statements);
export const ledgerEvents = (suffixes) => ({ payment, trace }) => {
  const pid = payment?.paymentId;
  const keys = suffixes.map((s) => (s ? `${pid}-${s}` : pid));
  return nonEmpty((trace?.allLedgerEvents || []).filter((e) => keys.includes(e.idempotencyKey)));
};
const camt026 = ({ payment }) =>
  nonEmpty((payment?.messages || []).filter((m) => m.purpose === "INVESTIGATION_REQUEST"));
const exceptionsOpened = ({ ownException, orphan, scenarioKey }) =>
  nonEmpty(scenarioKey === "R5" ? [orphan] : scenarioKey === "R2" ? [orphan, ownException] : [ownException]);
// The exception doc with the freshest agent{} (the stepper polls agent{} separately).
const followed = ({ followedException, agent }) =>
  followedException ? nonEmpty({ ...followedException, agent: agent ?? followedException.agent }) : null;

const W = (collection, op, fields, pick, via) => ({ collection, op, fields, pick, via });

const INITIATE = [
  W("payments", "insert", "The payment itself: lifecycle.currentState IN_PROGRESS, lifecycle.settlementStatus PENDING, checks[] from every stage, clearing.*",
    ({ payment }) => nonEmpty(paymentDoc(payment))),
  W("transactions", "insert", "The money movement, written in the same ACID transaction that debits the customer's account",
    ({ trace }) => nonEmpty(trace?.transaction)),
  W("accounts", "update", "Customer account debited, clearing account credited, in the same ACID transaction", () => null),
  W("routingSnapshots", "insert", "The routing decision: network, correspondent, cut-off, rationale",
    ({ payment }) => nonEmpty(payment?.routingSnapshot)),
  W("paymentExecutions", "insert", "The rail submission attempt", ({ payment }) => nonEmpty(payment?.executions)),
  W("paymentMessages", "insert", "The ISO 20022 pacs.008 sent, and the pacs.002 status received", pacsMessages),
  W("settlementPositions", "insert", "The expected settlement: expectedAmount, actualAmount null until the statement says otherwise", position),
  W("ledgerEvents", "insert", "Principal and fee events, idempotencyKey {paymentId} and {paymentId}-FEE",
    ledgerEvents(["", "FEE"]), "Ledger service, change stream on transactions"),
];

const SETTLE = [
  W("payments", "update", "lifecycle.currentState SETTLED, lifecycle.settlementStatus SETTLED, clearing.settledAt",
    ({ payment }) => nonEmpty(paymentDoc(payment))),
  W("settlementPositions", "update", "expectedWindow: when the statement line is due", position),
  W("ledgerEvents", "insert", "Settlement event {paymentId}-SETTLEMENT: clearing account to Nostro/Central Bank Cash",
    ledgerEvents(["SETTLEMENT"]), "Ledger service, change stream on payments"),
];

const GL_POST = [
  W("subLedgerEntries", "insert", "One entry per debit and credit side of each event",
    ({ trace }) => nonEmpty(trace?.subLedgerEntries)),
  W("journalEntries", "insert", "Balanced double-entry journals, same ACID transaction as the two updates below",
    ({ trace }) => nonEmpty(trace?.allJournalEntries)),
  W("ledgerEvents", "update", "postingStatus POSTED, postingResult.journalEntryId",
    ledgerEvents(["", "FEE", "SETTLEMENT"])),
  W("transactions", "update", "journalEntryId, postedAt", ({ trace }) => nonEmpty(trace?.transaction)),
  W("payments", "update", "lifecycle.postingStatus POSTED, refs.journalEntryId, refs.ledgerEventId",
    ({ payment }) => nonEmpty(paymentDoc(payment))),
];

const STATEMENT = [
  W("paymentMessages", "insert", "camt.053 statement: purpose ACCOUNT_STATEMENT, statement.window, opening and closing balance, entries[] (one per line)",
    statements),
];

const MATCH = [
  W("paymentMessages", "update", "entries[].recon on the statement: AUTO_MATCHED with matchedPaymentId, or an exceptionId for an unmatched line",
    statements),
  W("settlementPositions", "update", "actualAmount, actualBookedAt, sourceMessageRef: what the correspondent actually booked", position),
  W("reconciliationItems", "upsert", "The three-way check: legs[] (payment, ledger, statement) and overallResult",
    ({ trace }) => nonEmpty(trace?.reconciliationItem)),
  W("payments", "update", "lifecycle.reconciliationStatus RECONCILED or DISCREPANT",
    ({ payment }) => nonEmpty(paymentDoc(payment))),
  W("exceptions", "insert", "One per break: category, status OPEN, detail, subjectRef", exceptionsOpened,
    "Its insert wakes the agent through a change stream"),
];

const MISSING = [
  W("exceptions", "insert", "RECONCILIATION_MISSING: category, status OPEN, detail.expectedWindowBy",
    ({ ownException }) => nonEmpty(ownException)),
];

const INVESTIGATE = [
  W("exceptions", "update", "agent.cause, agent.evidence, agent.candidates, agent.proposedAction (or agent.nextCheckAt for a recheck)",
    followed, "Agent service"),
  W("checkpoints", "insert", "LangGraph checkpoints (database checkpointing_db, thread_id = exceptionId): how the paused run resumes after approval",
    () => null, "Agent service"),
];

// What Approve writes depends on the scenario's action.
const APPROVE_BY_SCENARIO = {
  R1: [
    W("ledgerEvents", "insert", "Adjustment {paymentId}-ADJ: Dr 5214 correspondent charges / Cr 1111 Nostro/Central Bank Cash, 25.00",
      ledgerEvents(["ADJ"]), "Ledger service, change stream"),
    W("settlementPositions", "update", "adjustmentPending", position),
    W("payments", "update", "clearing.settlementAdjustment", ({ payment }) => nonEmpty(paymentDoc(payment))),
  ],
  R1b: [
    W("payments", "update", "lifecycle.reconciliationStatus RECONCILED. No ledger entry, by design",
      ({ payment }) => nonEmpty(paymentDoc(payment))),
  ],
  R2: [
    W("paymentMessages", "update", "entries[].recon MANUAL_MATCHED on the altered line", statements),
    W("settlementPositions", "update", "actualAmount, sourceMessageRef from the linked line", position),
  ],
  R4: [
    W("paymentMessages", "insert", "camt.026 query to the correspondent: purpose INVESTIGATION_REQUEST", camt026),
  ],
  R5: [
    W("paymentMessages", "insert", "camt.026 query to the correspondent about the unknown line (escalation.paymentMessageId on the exception)",
      () => null),
  ],
};
// The simulated correspondent's answer to the escalation, shown on the final step.
const REPLY_WRITER = "Ledger service, correspondent reply (after 20s or on demand)";
const REPLY_BY_SCENARIO = {
  R4: [
    W("settlementPositions", "update", "actualAmount set to the confirmed amount, sourceMessageRef", position, REPLY_WRITER),
    W("exceptions", "update", "awaitingCounterparty false, escalation.reply, status RESOLVED (RECHECK)", followed, REPLY_WRITER),
    W("payments", "update", "lifecycle.reconciliationStatus RECONCILED, set by the tie-out",
      ({ payment }) => nonEmpty(paymentDoc(payment)), REPLY_WRITER),
  ],
  R5: [
    W("exceptions", "update", "awaitingCounterparty false, escalation.reply, status DISMISSED", followed, REPLY_WRITER),
  ],
};
const APPROVE_COMMON = W("exceptions", "update",
  "agent.approval, agent.actionsTaken[], agent.verification; status and resolution (or awaitingCounterparty and escalation for an escalation)",
  followed, "Agent service, then the resolve route");

const LATE = [
  ...STATEMENT,
  W("exceptions", "update", "status RESOLVED, resolution: the recheck found the line", followed, "Agent's scheduled recheck"),
];

const BY_STEP = {
  initiate: INITIATE,
  settle: SETTLE,
  glpost: GL_POST,
  statement: STATEMENT,
  match: MATCH,
  overdue: MISSING,
  window: MISSING,
  investigate: INVESTIGATE,
  late: LATE,
  verify: [],
};

export function writesFor(stepKey, scenarioKey) {
  if (stepKey === "verify") return REPLY_BY_SCENARIO[scenarioKey] || [];
  if (stepKey === "approve") return [APPROVE_COMMON, ...(APPROVE_BY_SCENARIO[scenarioKey] || [])];
  return BY_STEP[stepKey] || [];
}
