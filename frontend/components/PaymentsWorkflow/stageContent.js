// Per-stage copy and MongoDB writes for the lifecycle pane. Pure data and functions, so the
// wording can be reviewed against Doina's doc without reading component code.
//
// One line each: `business` says what happened in plain terms, `technical` says how, `why`
// says what MongoDB contributes. Inbound differs only where the stage itself differs.

import { fmtAmount, fmtWhen } from "@/lib/paymentsWorkflow/status";
import { legTotals } from "./lifecycleStages";
import {
  paymentDoc, nonEmpty, position, statements, ledgerEvents, historyDocs,
} from "../Showcase/stepWrites";

const OUT = "OUTBOUND";
const IN = "INBOUND";

const COPY = {
  initiation: {
    [OUT]: {
      business: "The customer's instruction is captured and the debtor and creditor are frozen as they were at that moment.",
      technical: "One ACID transaction inserts the payments document, with the parties embedded as an immutable snapshot, and its first paymentTimeline event.",
      why: "One payments document serves every rail: each carries wireDetails, achDetails and internalDetails, and only the envelope for its own rail is filled. No per-rail collection and no schema change."
    },
    [IN]: {
      business: "A payment message arrives from another bank and is recorded exactly as received.",
      technical: "The raw pacs.008 is stored as canonical JSON and one payments document is inserted for the inbound order.",
      why: "The raw message and the canonical payment sit side by side, with no schema change when a new field appears.",
    },
  },
  authentication: {
    [OUT]: {
      business: "The caller is verified and the payment is checked against the customer's entitlements and limits.",
      technical: "authentication{} and entitlement{} are embedded on the payments document; a step-up holds the payment at INITIATED.",
      why: "The payment keeps the authentication and entitlement results it was judged on, plus a reference to the session, so the decision and its evidence travel together without copying the identity system.",
    },
    [IN]: {
      business: "The beneficiary named in the message is matched to a real account at this bank.",
      technical: "An accounts lookup resolves the beneficiary; the result is persisted as beneficiaryResolution on the payment.",
      why: "Beneficiary resolution is a lookup on accounts in the same cluster as the payment, and its verdict is stored beside the claim it resolved, so both can be compared later.",
    },
  },
  validation: {
    [OUT]: {
      business: "The payment is checked for completeness and viability, and missing details such as FX and fees are filled in.",
      technical: "Validation and enrichment results are written back to the payments document. The funds hold is one ACID transaction: a conditional update moves the amount from balance.available to balance.hold only if available covers it, and marks the reservation on the payment.",
      why: "The conditional update makes the balance check and the hold one atomic step, so two payments cannot reserve the same funds. The enrichment record keeps each field's before and after, and the fx object carries rate provenance a single scalar could not.",
    },
    [IN]: {
      business: "The incoming payment is validated, screened and enriched before it can be accepted.",
      technical: "Structural checks, sanctions result and FX are written to validation{} and enrichment{} on the payment.",
      why: "Sanctions, FX and the corridor classification are written to the same payment document as the checks, so the audit trail needs no separate store.",
    },
  },
  authorization: {
    [OUT]: {
      business: "Fraud scoring clears the payment and the bank chooses the network and correspondent to send it through.",
      technical: "An immutable routingSnapshots document records the decision; fraud{} is embedded on the payment.",
      why: "routingSnapshots is insert-only, so a later change to a BIC or correspondent cannot rewrite why this route was chosen.",
    },
    [IN]: {
      business: "The bank decides whether to accept the incoming payment, after compliance checks.",
      technical: "The accept or reject decision, with its checks, is recorded on the payment and in paymentTimeline.",
      why: "Accept or reject is one acceptanceDecision object, rolled up from results already stored on the same document.",
    },
  },
  execution: {
    [OUT]: {
      business: "The payment is converted to the network's message format and sent.",
      technical: "A paymentExecutions document per attempt and a pacs.008 in canonicalJsonStorage, then the money move: one ACID transaction releases the hold, debits the debtor only if funds are available, credits the creditor, and inserts the transactions and notifications documents.",
      why: "The canonical payment and the ISO 20022 message are separate documents linked by id, and each attempt gets its own paymentExecutions document, so a new rail adds a mapping and a retry never overwrites history.",
    },
    [IN]: {
      business: "A confirmation goes back to the sending bank saying the payment was accepted or rejected.",
      technical: "A pacs.002 is written to canonicalJsonStorage; there is no paymentExecutions document for inbound.",
      why: "Inbound and outbound messages share one collection, told apart by direction.",
    },
  },
  "g:Accounting & Posting": {
    both: {
      business: "The bank's books record the money movement as balanced debits and credits.",
      technical: "A change stream on transactions writes the ledger event (unique idempotencyKey). A second change stream posts its sub-ledger entries in an ACID transaction. A scheduled batch aggregates posted entries into one balanced journal per period.",
      why: "Change streams resume from a stored token after a restart, unique indexes make every posting idempotent, and a $expr validator on journalEntries rejects any journal whose debits and credits differ.",
    },
  },
  "g:Clearing & Settlement": {
    both: {
      business: "The money actually moves between banks and the books record the settlement.",
      technical: "A settlementPositions document holds the expected amount; a change stream on the SETTLED flip writes the settlement ledger event.",
      why: "The expected and actual amounts live on one document, so a shortfall is visible without a join.",
    },
  },
  reconciliation: {
    both: {
      business: "The bank proves the payment, its ledger entries and the correspondent's statement all agree.",
      technical: "Statement lines are matched to positions, then a reconciliationItems document records the three-way result; a break opens an exception.",
      why: "The three legs of the match sit in one reconciliationItems document, and each statement line is claimed with a conditional update so it can match only one position.",
    },
  },
};

const stageKeyOf = (stage) => stage.key;

function copyFor(stage, direction) {
  const entry = COPY[stageKeyOf(stage)];
  if (!entry) return null;
  return entry[direction] || entry.both || null;
}

export function stageCopy(stage, direction) {
  return copyFor(stage, direction === IN ? IN : OUT);
}

const W = (collection, op, fields, pick, via) => ({ collection, op, fields, pick, via });

// Writes that commit or roll back together. Paths are relative to backend/; check them when
// the transaction body changes. The rail groups adjacent writes sharing one of these.
const TXN = {
  capture: {
    label: "Payment capture",
    source: "transactions/contexts/payment_order_initiation/domain/lifecycle.py:249-263",
  },
  fundsHold: {
    label: "Funds hold",
    source: "transactions/contexts/payment_order_initiation/application/funds_reservation.py:61-102",
  },
  moneyMove: {
    label: "Money move",
    source: "transactions/contexts/payment_rail/execute.py:508-618",
  },
  subLedger: {
    label: "Sub-ledger posting",
    source: "ledger/services/subledger_service.py:179-194",
  },
  journal: {
    label: "Journal posting",
    source: "ledger/services/journal_service.py:157-174",
  },
};
const inTxn = (txn, ...writes) => writes.map((w) => ({ ...w, txn }));

// Outbound's rail messages carry no `purpose`; inbound's stage-5 artifact is the outbound
// STATUS_RESPONSE (pacs.002), which does.
const executionMessages = ({ payment }) =>
  nonEmpty((payment?.messages || []).filter((m) => !m.purpose || m.purpose === "STATUS_RESPONSE"));

const whole = ({ payment }) => nonEmpty(paymentDoc(payment));
// Written at this stage but not returned by any endpoint this page reads.
const untracked = () => null;
untracked.untracked = true;

// One `paymentTimeline` row per stage: its lifecycle events and checks, as stored.
const HISTORY_FIELDS = "Lifecycle events and checks from this stage, one document each with a contiguous seq";
const history = (filter, fields = HISTORY_FIELDS) =>
  W("paymentTimeline", "insert", fields, ({ payment }) => historyDocs(payment, filter));

// Writes per lifecycle stage key. A panel inside a grouped stage uses its own key; a grouped
// stage's rail is the union of its panels.
const WRITES = {
  initiation: [
    ...inTxn(TXN.capture,
      W("payments", "insert", "The payment, with debtor and creditor embedded as an immutable snapshot", whole),
      history({ states: ["DRAFT", "INITIATED", "RECEIVED"] }, "The DRAFT event, inserted in the same transaction as the payment"),
    ),
  ],
  authentication: [
    W("payments", "update", "authentication{} and entitlement{}: what was checked and what it was judged against", whole),
    history({ stages: ["2"] }),
  ],
  validation: [
    W("payments", "update", "enrichment{} and validation{} from this stage; the checks go to paymentTimeline", whole),
    ...inTxn(TXN.fundsHold,
      W("payments", "update", "refs.fundsReservationId: the reservation marker", whole),
      W("accounts", "update", "balance.available to balance.hold, only if available covers the amount", untracked),
    ),
    history({ states: ["VALIDATED", "ENRICHED", "FINAL_VALIDATED"], stages: ["3"] }),
  ],
  authorization: [
    W("routingSnapshots", "insert", "The routing decision: network, correspondent, cut-off, rationale",
      ({ payment }) => nonEmpty(payment?.routingSnapshot)),
    W("payments", "update", "fraud{} and authorization", whole),
    history({ states: ["ROUTED", "MANUAL_FRAUD_REVIEW", "AUTHORISED", "APPROVED", "ACCEPTED", "REJECTED"], stages: ["4"] }),
  ],
  execution: [
    W("paymentExecutions", "insert", "The rail submission attempt", ({ payment }) => nonEmpty(payment?.executions)),
    W("canonicalJsonStorage", "insert", "The ISO 20022 message sent and the status received", executionMessages),
    ...inTxn(TXN.moneyMove,
      W("payments", "update", "Reservation marked released; an internal transfer also moves to SETTLED here (external wires stay IN_PROGRESS)", whole),
      W("accounts", "update", "Hold released, debtor debited only if funds are available, creditor credited", untracked),
      W("transactions", "insert", "The debit record", ({ trace }) => nonEmpty(trace?.transaction)),
      W("notifications", "insert", "Customer notifications", untracked),
    ),
    history({ states: ["SUBMITTED", "IN_PROGRESS"], stages: ["5"] }),
  ],
  ledgerEvent: [
    W("ledgerEvents", "insert", "Principal event, idempotencyKey {paymentId}", ledgerEvents([""]),
      "Ledger service, change stream on transactions"),
  ],
  feeLedgerEvent: [
    W("ledgerEvents", "insert", "Fee event, idempotencyKey {paymentId}-FEE", ledgerEvents(["FEE"]),
      "Ledger service, change stream on transactions"),
  ],
  subLedger: [
    ...inTxn(TXN.subLedger,
      W("subLedgerEntries", "insert", "One entry per debit and credit side of each event",
        ({ trace }) => nonEmpty(trace?.subLedgerEntries)),
      W("ledgerEvents", "update", "postingResult: the two sub-ledger ids (postingStatus stays PENDING)",
        ledgerEvents(["", "FEE"])),
    ),
  ],
  generalLedger: [
    ...inTxn(TXN.journal,
      W("journalEntries", "insert", "Balanced double-entry journals",
        ({ trace }) => nonEmpty(trace?.allJournalEntries)),
      W("subLedgerEntries", "update", "journalEntryId stamped on every entry the journal covers",
        ({ trace }) => nonEmpty(trace?.subLedgerEntries)),
      W("ledgerEvents", "update", "postingStatus POSTED, postingResult.journalEntryId",
        ledgerEvents(["", "FEE"])),
    ),
    history({ states: ["POSTED"] }, "The POSTED event, written by the ledger service in the same transaction as the state change"),
  ],
  settlementConfirm: [
    W("settlementPositions", "insert", "Expected amount, window and settled status", position),
    history({ states: ["SETTLED"], stages: ["7"] }),
  ],
  settlementPosting: [
    W("ledgerEvents", "insert", "Settlement event {paymentId}-SETTLEMENT: clearing account to Nostro/Central Bank Cash",
      ledgerEvents(["SETTLEMENT"]), "Ledger service, change stream on payments"),
  ],
  reconciliation: [
    W("canonicalJsonStorage", "update", "camt.053 statement lines matched to this payment", statements),
    W("reconciliationItems", "upsert", "The three-way check: legs[] and overallResult",
      ({ trace }) => nonEmpty(trace?.reconciliationItem)),
    history({ states: ["RECONCILED"], stages: ["8"] }, "The RECONCILED event, written by the ledger service"),
  ],
};

// Inbound has no rail submission, so stage 5 swaps the outbound rows: paymentExecutions
// becomes an explicit "not applicable" card (shown rather than hidden, so the absence is
// visible) and canonicalJsonStorage describes the pacs.002 status response.
const INBOUND_WRITES = {
  // Funds are reserved against a customer debit; inbound has none.
  validation: [
    W("payments", "update", "enrichment{} and validation{} from this stage; the checks go to paymentTimeline", whole),
    history({ states: ["VALIDATED", "ENRICHED", "FINAL_VALIDATED"], stages: ["3"] }),
  ],
  execution: [
    {
      collection: "paymentExecutions",
      notApplicable: true,
      fields: "Not applicable to inbound payments: no rail submission. The status response is stored in canonicalJsonStorage.",
      pick: () => null,
    },
    W("canonicalJsonStorage", "insert", "The pacs.002 status response sent to the sending bank", executionMessages),
  ],
};

/** The writes behind a stage, de-duplicated by collection and operation for grouped stages. */
export function stageWrites(stage, direction) {
  const panels = stage.children?.length > 1 ? stage.children : [stage];
  const writesOf = (p) => (direction === IN && INBOUND_WRITES[p.key]) || WRITES[p.key] || [];
  const seen = new Set();
  return panels.flatMap(writesOf).filter((w) => {
    const id = `${w.collection}|${w.op}|${w.fields}`;
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

// Aggregation pipelines the backend runs at a stage, copied from the source named in `source`.
// They are hand-copied, not fetched: update them when that source changes.
const Q = (collection, title, when, source, pipeline) => ({ collection, title, when, source, pipeline });

const QUERIES = {
  generalLedger: [
    Q("subLedgerEntries", "Completeness check", "GL batch, before each journal is written",
      "ledger/services/journal_service.py:210", `db.subLedgerEntries.aggregate([
  { $match: { status: "POSTED", journalEntryId: "" } },
  { $group: {
      _id: "$sourceReference.sourceId",
      count: { $sum: 1 },
      sides: { $addToSet: "$side" },
      periodCodes: { $addToSet: "$periodCode" }
  } }
])
// An event enters the journal only with exactly one DEBIT and one CREDIT in one period.`),
    Q("subLedgerEntries", "Journal summary", "GL batch, one balanced journal per period",
      "ledger/services/journal_service.py:241", `db.subLedgerEntries.aggregate([
  { $match: {
      status: "POSTED",
      journalEntryId: "",
      "sourceReference.sourceId": { $in: completeEventIds }
  } },
  { $group: {
      _id: { periodCode: "$periodCode", controlAccountCode: "$controlAccountCode", side: "$side" },
      amount: { $sum: "$amount" },
      currency: { $first: "$currency" },
      count: { $sum: 1 },
      subLedgerIds: { $push: "$subLedgerId" },
      eventIds: { $addToSet: "$sourceReference.sourceId" },
      eventTypes: { $addToSet: "$eventType" }
  } }
])`),
  ],
  reconciliation: [
    Q("canonicalJsonStorage", "Candidate statement lines", "Reconciliation agent, when a break opens an exception",
      "payment_agent/recon_evidence.py:117", `db.canonicalJsonStorage.aggregate([
  { $match: {
      purpose: STATEMENT_PURPOSE,
      "statement.accountCode": position.settlementAccountCode,
      "statement.historical": { $ne: true },
      "entries.recon.status": "UNMATCHED"
  } },
  { $unwind: "$entries" },
  { $match: {
      "entries.recon.status": "UNMATCHED",
      "entries.amount": { $gte: expected - tolerance, $lte: expected + tolerance }
  } },
  { $project: {
      _id: 0, paymentMessageId: 1,
      lineNo: "$entries.lineNo", reference: "$entries.reference",
      amount: "$entries.amount", charges: "$entries.charges",
      bookingDate: "$statement.window.to"
  } }
])`),
    Q("settlementPositions", "Candidate payments for a statement line", "Reconciliation agent, when a break opens an exception",
      "payment_agent/recon_evidence.py:142", `db.settlementPositions.aggregate([
  { $match: {
      settlementAccountCode: statement.accountCode,
      actualAmount: null,
      expectedAmount: { $gte: amount - tolerance, $lte: amount + tolerance }
  } },
  { $lookup: {
      from: "payments", localField: "paymentId", foreignField: "paymentId",
      as: "payment",
      pipeline: [{ $project: { _id: 0, lifecycle: 1 } }]
  } },
  { $match: {
      "payment.lifecycle.currentState": { $in: ["SETTLED", "POSTED"] },
      "payment.lifecycle.reconciliationStatus": { $ne: "RECONCILED" }
  } },
  { $project: { _id: 0, paymentId: 1, expectedAmount: 1 } },
  { $limit: 50 }
])`),
  ],
};

/** The aggregation pipelines behind a stage; a grouped stage gets the union of its panels'. */
export function stageQueries(stage) {
  const panels = stage.children?.length > 1 ? stage.children : [stage];
  return panels.flatMap((p) => QUERIES[p.key] || []);
}

// The MongoDB capabilities a stage relies on, one chip each. Only what the code does: every
// entry names the backend/ path that proves it, so check it when that code changes.
// `only` limits a chip to one direction.
const C = (label, detail, source, only) => ({ label, detail, source, only });

const CAPABILITIES = {
  initiation: [
    C("ACID transaction · 2 collections",
      "The payments document and its DRAFT paymentTimeline event commit together, so a rejected duplicate leaves no orphan event.",
      TXN.capture.source),
  ],
  validation: [
    C("ACID transaction · 2 collections",
      "The reservation marker on payments and the hold on accounts commit together.",
      TXN.fundsHold.source, OUT),
    C("Conditional update",
      "The hold matches only if balance.available >= amount, so the balance check and the hold are one atomic step.",
      "transactions/contexts/payment_order_initiation/application/funds_reservation.py:89-96", OUT),
  ],
  authorization: [
    C("Live counts on payments",
      "Fraud features such as prior payments to this beneficiary and the debtor's payments in the window are count_documents queries on payments at scoring time.",
      "transactions/contexts/fraud_evaluation/evaluate.py:539-582", OUT),
  ],
  execution: [
    C("ACID transaction · 4 collections",
      "Releasing the hold, debiting, crediting, and inserting the transaction and notifications commit or roll back together.",
      TXN.moneyMove.source, OUT),
    C("Conditional debit",
      "The debit matches only if the debtor's available balance covers it; no match aborts the whole transaction.",
      "transactions/contexts/payment_rail/execute.py:521-538", OUT),
  ],
  ledgerEvent: [
    C("Change stream · persisted resume token",
      "The ledger watches transactions and stores its resume token in changeStreamTokens, so a restart picks up where it stopped.",
      "ledger/workers/ingest_worker.py:216-229"),
    C("Unique index · idempotencyKey",
      "A replayed event hits the unique index and is skipped, so it posts once.",
      "ledger/data/ensure_indexes.py:33"),
    C("$expr validator",
      "ledgerEvents rejects any document whose debit and credit amounts differ.",
      "ledger/data/ensure_indexes.py:205-207"),
  ],
  subLedger: [
    C("ACID transaction · 2 collections",
      "Sub-ledger entries and the event's postingResult commit together.",
      TXN.subLedger.source),
  ],
  generalLedger: [
    C("ACID transaction · 3 collections",
      "The journal, the journalEntryId on each sub-ledger entry and POSTED on each event commit together.",
      TXN.journal.source),
    C("$expr validator",
      "journalEntries rejects any journal whose debit total differs from its credit total.",
      "ledger/data/ensure_indexes.py:212-247"),
  ],
  settlementPosting: [
    C("Change stream · persisted resume token",
      "The ledger watches payments for the SETTLED flip and stores its resume token in changeStreamTokens.",
      "ledger/workers/settlement_worker.py:65-78"),
  ],
  reconciliation: [
    C("Partial index",
      "Indexes only ACCOUNT_STATEMENT documents by their line status, the matcher's read path.",
      "ledger/data/ensure_indexes.py:253-259"),
    C("$lookup",
      "The reconciliation agent joins candidate settlement positions to their payments in one pipeline.",
      "payment_agent/recon_evidence.py:142"),
  ],
};

/** Capability chips for a stage; a grouped stage gets the union of its panels', de-duplicated. */
export function stageCapabilities(stage, direction) {
  const dir = direction === IN ? IN : OUT;
  const panels = stage.children?.length > 1 ? stage.children : [stage];
  const seen = new Set();
  return panels.flatMap((p) => CAPABILITIES[p.key] || []).filter((c) => {
    const id = `${c.label}|${c.source}`;
    if ((c.only && c.only !== dir) || seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

/** Documents available now for a list of writes, for the collapsed rail's summary. */
export function writeSummary(writes, sources) {
  // A stage can write one document twice (payments in two transactions); count it once.
  const docKey = (w, d) => `${w.collection}:${d._id ?? d.paymentId ?? JSON.stringify(d)}`;
  const withDocs = writes.filter((w) => w.pick(sources));
  const documents = new Set(withDocs.flatMap((w) => w.pick(sources).map((d) => docKey(w, d))));
  return {
    documents: documents.size,
    collections: new Set(withDocs.map((w) => w.collection)).size,
  };
}

/**
 * Expected minus booked, for a settlement position. Null until the correspondent's statement
 * has booked an amount; 0 when they agree to the cent.
 */
export function settlementDelta(position) {
  const expected = position?.expectedAmount ?? position?.grossAmount;
  const actual = position?.actualAmount;
  if (expected == null || actual == null) return null;
  const delta = Number(expected) - Number(actual);
  return {
    expected: Number(expected),
    actual: Number(actual),
    delta: Math.abs(delta) < 0.005 ? 0 : delta,
  };
}

const present = (v) => v != null && v !== "" && v !== "—";
const yesNo = (v) => (v ? "Yes" : "No");
const child = (stage, key) => stage.children?.find((c) => c.key === key);

function gateChecksHeadline(checks) {
  if (!checks?.length) return null;
  const count = (r) => checks.filter((c) => c.result === r).length;
  const passed = count("PASS");
  const rest = [["warning", count("WARN")], ["failed", count("FAIL")], ["skipped", count("SKIP")]]
    .filter(([, n]) => n)
    .map(([label, n]) => `${n} ${label}`);
  return `${passed} of ${checks.length} passed${rest.length ? ` · ${rest.join(" · ")}` : ""}`;
}

// Each entry: [label, value, tone]. tone "warn" marks a value that needs a second look.
function factsFor(stage, payment) {
  const d = stage.data;
  const ccy = payment?.currency;
  switch (stage.key) {
    case "initiation":
      return [
        ["Payment", d?.paymentId],
        ["Rail", d?.rail],
        ["Amount", fmtAmount(d?.amount, d?.currency)],
        ["Charge bearer", d?.chargeBearer],
        ["Execution date", d?.requestedExecutionDate],
        ["Channel", d?.initiation?.channel],
      ];
    case "authentication": {
      if (stage.kind === "beneficiaryResolution") {
        return [
          ["Match outcome", d?.matchOutcome],
          ["Matched account", d?.matchedAccountId],
          ["Match method", d?.matchMethod],
        ];
      }
      const a = payment?.authentication;
      const e = payment?.entitlement;
      return [
        ["Gate checks", gateChecksHeadline(d)],
        ["Authentication", a?.method],
        ["Step-up", a ? yesNo(a.stepUp) : null],
        ["Segment", e?.segment],
        ["Per-payment limit", e?.perPaymentLimit != null ? fmtAmount(e.perPaymentLimit, ccy) : null],
        ["Dual approval", e ? (e.dualApprovalRequired ? `Required · ${e.dualApprovalBy || "—"}` : "Not required") : null],
      ];
    }
    case "validation": {
      const checks = d?.checks || [];
      const warned = checks.filter((c) => c.result === "WARN").length;
      const failed = checks.filter((c) => c.result === "FAIL").length;
      return [
        ["Fields enriched", d?.enrichment?.resolved?.length],
        ["FX rate", payment?.fxRate],
        ["Charges", payment?.fees?.length
          ? payment.fees.map((f) => `${fmtAmount(f.amount, f.currency)} ${f.type}`).join(", ")
          : null],
        ["Purpose", payment?.categoryPurpose],
        ["Warnings", warned || null, "warn"],
        ["Refusals", failed || null, "warn"],
      ];
    }
    case "authorization": {
      if (stage.kind === "acceptanceDecision" || payment?.acceptanceDecision) {
        const ad = payment?.acceptanceDecision;
        return [
          ["Decision", ad?.decision],
          ["Reason code", ad?.reasonCode],
          ["Beneficiary match", ad?.beneficiaryMatch],
          ["Sanctions/AML", ad?.sanctionsStatus],
        ];
      }
      const f = d?.fraud;
      return [
        ["Fraud decision", f?.decision],
        ["Fraud score", f?.score != null ? `${f.score}/100` : null],
        ["Clearing network", d?.network],
        ["Sanctions/AML", d?.sanctions?.status],
        ["Confirmation", payment?.confirmation?.confirmationId],
      ];
    }
    case "execution": {
      const e = d?.execution;
      return [
        ["Message", e ? `${e.messageStandard} ${e.messageFormat}` : null],
        ["Network", e?.clearingNetwork || payment?.rail],
        ["Execution status", e?.status],
        ["Attempt", e ? `${e.attempt} of ${d?.attempts?.length || 1}` : null],
        ["Network ref", d?.clearing?.networkRef],
        ["Simulated rail", e ? yesNo(e.simulated) : null],
      ];
    }
    case "g:Accounting & Posting": {
      const ev = child(stage, "ledgerEvent");
      const sub = child(stage, "subLedger")?.data;
      const totals = ev?.legs ? legTotals(ev.legs) : null;
      return [
        ["Posting status", ev?.status],
        ["Journal entry", ev?.data?.postingResult?.journalEntryId],
        ["Period", sub?.[0]?.periodCode],
        ["Sub-ledger entries", sub?.length],
        ["Debits = credits", totals
          ? (totals.balanced ? fmtAmount(totals.debit, ev.legs.currency) : "Not balanced")
          : null,
          totals && !totals.balanced ? "warn" : undefined],
      ];
    }
    case "g:Clearing & Settlement": {
      const conf = child(stage, "settlementConfirm")?.data;
      const pos = conf?.position;
      const delta = settlementDelta(pos);
      const pccy = pos?.currency || ccy;
      return [
        ["Outcome", pos?.outcome],
        ["Settlement status", pos?.settlementStatus || payment?.lifecycle?.settlementStatus],
        ["Expected", pos?.expectedAmount != null ? fmtAmount(pos.expectedAmount, pccy) : null],
        ["Booked", delta ? fmtAmount(delta.actual, pccy) : null],
        ["Short by", delta?.delta ? fmtAmount(delta.delta, pccy) : null, "warn"],
        ["Settled", conf?.clearing?.settledAt ? fmtWhen(conf.clearing.settledAt) : null],
      ];
    }
    case "reconciliation": {
      const check = d?.check;
      const delta = settlementDelta(d?.position);
      return [
        ["Overall", check?.overallResult || "not yet checked"],
        ["Short by", delta?.delta ? fmtAmount(delta.delta, d?.position?.currency || ccy) : null, "warn"],
        ["Settlement model", d?.position?.modelLabel],
        ["Checked", check?.checkedAt ? fmtWhen(check.checkedAt) : null],
      ];
    }
    default:
      return [];
  }
}

/** Four to six label/value facts a business reader needs, empty ones dropped. */
export function stageFacts(stage, payment) {
  return factsFor(stage, payment).filter(([, value]) => present(value));
}

// BIAN service domains per stage, as Doina's document names them. `operation` is only set where
// it was checked against the BIAN v14 index (stage 1: Initiate), never guessed.
const BIAN = {
  initiation: {
    [OUT]: { domains: ["Payment Order Initiation"], operation: "POST /PaymentOrderInitiation/Initiate" },
    [IN]: {
      domains: ["Payment Order Initiation", "Financial Gateway"],
      operation: "POST /FinancialGateway/{id}/Inbound/Initiate",
    },
  },
  authentication: {
    [OUT]: { domains: ["Payment Order Initiation", "Party Authentication", "Current Account"] },
    [IN]: { domains: ["Financial Gateway", "Current Account", "Party Reference Data Directory"] },
  },
  validation: {
    [OUT]: { domains: ["Payment Order Initiation"] },
    [IN]: { domains: ["Payment Order Initiation", "Current Account"] },
  },
  authorization: {
    [OUT]: { domains: ["Payment Orchestration", "Payment Confirmation", "Fraud Evaluation"] },
    [IN]: { domains: ["Payment Confirmation", "Financial Crime Evaluation"] },
  },
  execution: { both: { domains: ["Financial Gateway", "Payment Rail"] } },
  "g:Accounting & Posting": {
    both: { domains: ["Financial Accounting", "Position Keeping", "Current Account"] },
  },
  "g:Clearing & Settlement": {
    [OUT]: {
      domains: ["Internal Bank Account", "Payment Settlement", "Correspondent Bank Directory"],
    },
    [IN]: {
      domains: ["Internal Bank Account", "Payment Settlement", "Correspondent Bank Directory"],
    },
  },
  reconciliation: { both: { domains: ["Account Reconciliation", "Payment Rail"] } },
};

/** `{ domains, operation? }` for a stage, or null when the document names none. */
export function stageBian(stage, direction) {
  const entry = BIAN[stage.key];
  if (!entry) return null;
  return entry[direction === IN ? IN : OUT] || entry.both || null;
}
