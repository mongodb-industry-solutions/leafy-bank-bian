// Per-stage copy and MongoDB writes for the lifecycle pane. Pure data and functions, so the
// wording can be reviewed against Doina's doc without reading component code.
//
// One line each: `business` says what happened in plain terms, `technical` says how, `why`
// says what MongoDB contributes. Inbound differs only where the stage itself differs.

import { fmtAmount, fmtWhen } from "@/lib/paymentsWorkflow/status";
import { legTotals } from "./lifecycleStages";
import {
  paymentDoc, nonEmpty, position, statements, ledgerEvents,
} from "../Showcase/stepWrites";

const OUT = "OUTBOUND";
const IN = "INBOUND";

const COPY = {
  initiation: {
    [OUT]: {
      business: "The customer's instruction is captured and the debtor and creditor are frozen as they were at that moment.",
      technical: "One insert into payments, with the parties embedded as an immutable snapshot and the idempotency key enforced by a unique index.",
      why: "One payments document serves every rail: a wire adds a wireDetails envelope and an internal transfer adds internalDetails. No per-rail collection and no schema change.",
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
      why: "Beneficiary resolution is an indexed lookup on accounts in the same cluster as the payment, and its verdict is stored beside the claim it resolved, so both can be compared later.",
    },
  },
  validation: {
    [OUT]: {
      business: "The payment is checked for completeness and viability, and missing details such as FX and fees are filled in.",
      technical: "$jsonSchema validation, then enrichment{} and validation{} written back to the payments document.",
      why: "Required fields are enforced by the database's own $jsonSchema validator, the enrichment record keeps each field's before and after, and the fx object carries rate provenance a single scalar could not.",
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
      technical: "The accept or reject decision, with its checks, is recorded on the payment and in lifecycle.events.",
      why: "Accept or reject is one acceptanceDecision object, rolled up from results already stored on the same document.",
    },
  },
  execution: {
    [OUT]: {
      business: "The payment is converted to the network's message format and sent.",
      technical: "A paymentExecutions document per attempt and a pacs.008 in paymentMessages, built from the canonical payment.",
      why: "The canonical payment and the ISO 20022 message are separate documents linked by id, and each attempt gets its own paymentExecutions document, so a new rail adds a mapping and a retry never overwrites history.",
    },
    [IN]: {
      business: "A confirmation goes back to the sending bank saying the payment was accepted or rejected.",
      technical: "A pacs.002 is written to paymentMessages; there is no paymentExecutions document for inbound.",
      why: "Inbound and outbound messages share one collection, told apart by direction.",
    },
  },
  "g:Accounting & Posting": {
    both: {
      business: "The bank's books record the money movement as balanced debits and credits.",
      technical: "A ledger event is written, a change stream posts its sub-ledger entries and one journal in an ACID transaction, keyed by idempotencyKey.",
      why: "Multi-document ACID transactions keep debits equal to credits, and change streams run the posting without polling.",
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
      why: "The three legs of the match sit in one document and a $lookup brings the evidence together.",
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
// Outbound's rail messages carry no `purpose`; inbound's stage-5 artifact is the outbound
// STATUS_RESPONSE (pacs.002), which does.
const executionMessages = ({ payment }) =>
  nonEmpty((payment?.messages || []).filter((m) => !m.purpose || m.purpose === "STATUS_RESPONSE"));

const whole = ({ payment }) => nonEmpty(paymentDoc(payment));

// Writes per lifecycle stage key. A panel inside a grouped stage uses its own key; a grouped
// stage's rail is the union of its panels.
const WRITES = {
  initiation: [
    W("payments", "insert", "The payment, with debtor and creditor embedded as an immutable snapshot", whole),
  ],
  authentication: [
    W("payments", "update", "authentication{} and entitlement{}: what was checked and what it was judged against", whole),
  ],
  validation: [
    W("payments", "update", "enrichment{}, validation{} and checks[] from this stage", whole),
  ],
  authorization: [
    W("routingSnapshots", "insert", "The routing decision: network, correspondent, cut-off, rationale",
      ({ payment }) => nonEmpty(payment?.routingSnapshot)),
    W("payments", "update", "fraud{} and authorization checks", whole),
  ],
  execution: [
    W("paymentExecutions", "insert", "The rail submission attempt", ({ payment }) => nonEmpty(payment?.executions)),
    W("paymentMessages", "insert", "The ISO 20022 message sent and the status received", executionMessages),
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
    W("subLedgerEntries", "insert", "One entry per debit and credit side of each event",
      ({ trace }) => nonEmpty(trace?.subLedgerEntries)),
  ],
  generalLedger: [
    W("journalEntries", "insert", "Balanced double-entry journals, posted in one ACID transaction",
      ({ trace }) => nonEmpty(trace?.allJournalEntries)),
  ],
  settlementConfirm: [
    W("settlementPositions", "update", "Expected amount, window and settled status", position),
  ],
  settlementPosting: [
    W("ledgerEvents", "insert", "Settlement event {paymentId}-SETTLEMENT: clearing account to Nostro/Central Bank Cash",
      ledgerEvents(["SETTLEMENT"]), "Ledger service, change stream on payments"),
  ],
  reconciliation: [
    W("paymentMessages", "update", "camt.053 statement lines matched to this payment", statements),
    W("reconciliationItems", "upsert", "The three-way check: legs[] and overallResult",
      ({ trace }) => nonEmpty(trace?.reconciliationItem)),
  ],
};

/** The writes behind a stage, de-duplicated by collection and operation for grouped stages. */
export function stageWrites(stage) {
  const panels = stage.children?.length > 1 ? stage.children : [stage];
  const seen = new Set();
  return panels.flatMap((p) => WRITES[p.key] || []).filter((w) => {
    const id = `${w.collection}|${w.op}|${w.fields}`;
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

/** Documents available now for a list of writes, for the collapsed rail's summary. */
export function writeSummary(writes, sources) {
  const withDocs = writes.filter((w) => w.pick(sources));
  return {
    documents: withDocs.reduce((n, w) => n + w.pick(sources).length, 0),
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
          ["Sanctions", ad?.sanctionsStatus],
        ];
      }
      const f = d?.fraud;
      return [
        ["Fraud decision", f?.decision],
        ["Fraud score", f?.score != null ? `${f.score}/100` : null],
        ["Clearing network", d?.network],
        ["Sanctions", d?.sanctions?.status],
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
