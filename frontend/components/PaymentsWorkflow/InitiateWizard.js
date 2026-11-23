"use client";

// Create Payment — bank-assisted initiation (doc 16 §7b).
//
// One comprehensive "Enter details" screen, not a four-step drip. Three numbered sections,
// and the split between them mirrors the request contract rather than being cosmetic:
//
//   1. Select Payment Type      → `rail` (and which envelope section 3 renders)
//   2. Payment Details          → the CANONICAL common layer, shared by every rail
//   3. Payment Type Details     → the rail envelope (`wireDetails` / `internalDetails`)
//                                 plus the creditor fields only that rail needs
//
// That is the same seam `api_models.PaymentOrderInitiateRequest` draws, so a field's
// position on screen tells you where it lands in the document.
//
// Doina's mockup is customer self-service; this is the bank-assisted variant an employee
// drives, which per her own Frontend research differs only at the initiation layer —
// hence the Customer field at the top of section 2 and `channel: "BRANCH"`.
import { useMemo, useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Banner from "@leafygreen-ui/banner";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import TextInput from "@leafygreen-ui/text-input";
import { Select, Option } from "@leafygreen-ui/select";
import Stepper, { Step } from "@leafygreen-ui/stepper";
import { Body, H2 } from "@leafygreen-ui/typography";
import Tooltip from "@leafygreen-ui/tooltip";

import styles from "./PaymentsWorkflow.module.css";
import { coreApi } from "@/lib/api/client";
import { useBankAssistedParties, useSubmitKey } from "@/lib/api/hooks";
import { fmtAmount } from "@/lib/paymentsWorkflow/status";

// Stepper stops at Confirmation. An "Approve" step is deliberately absent: dual approval
// is stage 2 (doc 15 B4) and has neither a threshold nor an approver today, so showing it
// would promise a gate that does not run.
const STEPS = ["Enter details", "Review", "Confirmation"];

// Wires and internal transfers are live. The unavailable rails are listed rather than
// hidden so the demo can point at the roadmap — `disabled` keeps them unselectable.
const PAYMENT_TYPES = [
  { rail: "WIRE", label: "Wires", available: true, glyph: "Building",
    blurb: "Real-time or near-real-time transfer of funds between banks." },
  { rail: "INTERNAL", label: "Internal Transfer", available: true, glyph: "Refresh",
    blurb: "Book transfer between two Leafy Bank accounts. Settles on our own ledger." },
  { rail: "ACH", label: "ACH", available: false, glyph: "Menu",
    blurb: "Batch clearing house transfers." },
  { rail: "CARD", label: "Cards", available: false, glyph: "CreditCard",
    blurb: "Card acquiring and issuing rails." },
];

// Contract values are ISO 20022 codes; the labels spell out who actually pays.
const CHARGE_BEARERS = [
  ["DEBT", "DEBT — Sender pays all charges"],
  ["CRED", "CRED — Beneficiary pays all charges"],
  ["SHAR", "SHAR — Shared between both parties"],
  ["SLEV", "SLEV — Following service level"],
];

const CLEARING_SYSTEMS = ["USABA", "USPID", "GBDSC", "CHBCC", "DEBLZ", "CACPA"];
const ACCOUNT_TYPES = ["Checking", "Savings", "Current", "FixedDeposit"];
const PRIORITIES = ["NORMAL", "HIGH", "URGENT"];

// Stage 7 FR-7.3 — the four simulated settlement outcomes (Doina Sep 17). A demo lever on
// the back-office wizard so all four are drivable from the screen and their distinct
// downstream behaviour is observable. MATCHED is the happy path; the others fork the
// lifecycle (DELAYED → PENDING, UNMATCHED → settles, the correspondent's statement books it
// $25 short (FEE_DEDUCTED alias) → stage 8 discrepancy,
// EXCEPTION → RETURNED).
// Internal transfers settle atomically in stage 5 and ignore this — the control is wire-only.
const SETTLEMENT_OUTCOMES = [
  { value: "MATCHED", label: "Matched", description: "Response confirms expected amount/account — SETTLED" },
  // DELAYED disabled — no real external rail to wait on, so a PENDING hold with no path to
  // resolve it reads as a stuck payment in the demo. Re-enable when a rail re-query hook lands.
  // { value: "DELAYED", label: "Delayed", description: "No confirmation yet within the window — PENDING" },
  { value: "UNMATCHED", label: "Fee deducted by correspondent", description: "Settles, but the statement books it $25 short — reconciliation flags the gap" },
  { value: "EXCEPTION", label: "Exception", description: "Network/correspondent rejected — RETURNED" },
];
// Reconciliation plan A1 — how the correspondent's camt.053 statement books this wire.
// Mirrors `StatementOutcomeLiteral` (api_models.py).
const STATEMENT_OUTCOMES = [
  { value: "CLEAN", label: "Clean", description: "Statement line matches our books exactly" },
  { value: "FEE_DEDUCTED", label: "Fee deducted", description: "Correspondent takes $25 from the principal" },
  { value: "REFERENCE_ALTERED", label: "Reference altered", description: "Correspondent re-keys our reference" },
  { value: "LATE", label: "Late", description: "Line is missing today, booked on the next statement" },
  { value: "AMOUNT_TRANSPOSED", label: "Amount transposed", description: "Two digits swapped — a keying error" },
];
const TRANSFER_TYPES = [
  ["OWN_ACCOUNT", "Own account"],
  ["THIRD_PARTY", "Third party"],
];

const EMPTY = {
  rail: "WIRE",
  // canonical
  customerId: "",
  debtorAccountId: "",
  currency: "USD",
  amount: "",
  valueDate: "",
  purpose: "",
  endToEndReference: "",
  clientReference: "",
  chargeBearer: "SLEV",
  // creditor — external
  beneficiaryName: "",
  beneficiaryAccountNo: "",
  beneficiaryAddress: "",
  beneficiaryCountry: "",
  // creditor — internal
  creditorAccountId: "",
  // wire envelope
  beneficiaryBankName: "",
  bic: "",
  clearingSystemMemberId: "",
  clearingSystemCode: "",
  accountNumberType: "Checking",
  priority: "NORMAL",
  serviceLevelCode: "",
  localInstrumentCode: "",
  // internal envelope
  transferType: "THIRD_PARTY",
  // stage 7 simulation lever (wire-only; default MATCHED = happy path)
  simulatedSettlementOutcome: "MATCHED",
  // reconciliation plan A1 lever (wire-only; default CLEAN)
  simulatedStatementOutcome: "CLEAN",
};

const isWire = (form) => form.rail === "WIRE";

// A wire whose Demo Controls make the correspondent deduct its $25 charge in flight.
const feeTakenInFlight = (form) =>
  isWire(form) &&
  (form.simulatedStatementOutcome === "FEE_DEDUCTED" ||
    form.simulatedSettlementOutcome === "UNMATCHED");

function buildPayload(form) {
  const payload = {
    customerId: form.customerId,
    type: "CREDIT_TRANSFER",
    rail: form.rail,
    debtor: { accountId: form.debtorAccountId },
    instructedAmount: Number(form.amount),
    instructedCurrency: form.currency.toUpperCase(),
    priority: form.priority,
    chargeBearer: form.chargeBearer,
    // The defining difference between this surface and the customer portal.
    channel: "BRANCH",
    // No `authentication` object. The operator's OPERATOR token, issued at login and
    // attached by `client.js`, is the assertion — and being an operator token it is what
    // permits initiating for a customer other than itself, which a customer token cannot
    // do. Stage 2 records `callerType: OPERATOR` on the payment.
  };

  if (form.valueDate) payload.requestedExecutionDate = form.valueDate;

  // DR-1.1: the customer's own internal tracking reference (PO number, contract ID),
  // distinct from endToEndId. The backend threads this through to the `payments` doc.
  // The same value is also mirrored into `remittance.invoiceNo` below, because the deep-dive
  // panel ("Client reference") and the pacs.008 remittance mapper read that field today.
  // Split these into separate inputs if a demo ever needs both distinct.
  if (form.clientReference) payload.clientReference = form.clientReference;

  const remittance = {
    unstructured: form.purpose || null,
    reference: form.endToEndReference || null,
    invoiceNo: form.clientReference || null,
  };
  if (Object.values(remittance).some(Boolean)) {
    payload.remittance = Object.fromEntries(
      Object.entries(remittance).filter(([, v]) => v)
    );
  }

  if (isWire(form)) {
    // External creditor: it carries its own identity. The contract requires name +
    // accountNo, and a BIC once there is no accountId.
    const creditor = {
      name: form.beneficiaryName,
      accountNo: form.beneficiaryAccountNo,
      bic: form.bic.toUpperCase(),
      bankName: form.beneficiaryBankName,
      bankCountry: form.beneficiaryCountry ? form.beneficiaryCountry.toUpperCase() : null,
      address: form.beneficiaryAddress,
      accountType: form.accountNumberType,
      clearingSystemCode: form.clearingSystemCode,
      clearingSystemMemberId: form.clearingSystemMemberId,
    };
    payload.creditor = Object.fromEntries(Object.entries(creditor).filter(([, v]) => v));

    const pti = {};
    if (form.serviceLevelCode) pti.serviceLevel = { code: form.serviceLevelCode };
    if (form.localInstrumentCode) pti.localInstrument = { code: form.localInstrumentCode };
    // `wireType` is deliberately NOT sent — the server derives it from the two bank
    // countries and nulls it when either is unknown (doc 13, initiation_envelope.py).
    // serviceLevel/localInstrument nest under paymentTypeInformation per the Pydantic
    // WireDetailsBody contract (extra="forbid") — flat placement 422's.
    if (Object.keys(pti).length) payload.wireDetails = { paymentTypeInformation: pti };

    // Stage 7 simulation lever (FR-7.3) — re-enabled for stage 9 (doc 24 §3 step 8): the
    // exceptions queue needs the non-happy settlement paths reachable from the wizard.
    payload.simulatedSettlementOutcome = form.simulatedSettlementOutcome;
    payload.simulatedStatementOutcome = form.simulatedStatementOutcome;
  } else {
    // An account we hold — name, BIC and address resolve from the snapshot server-side.
    payload.creditor = { accountId: form.creditorAccountId };
    payload.internalDetails = { transferType: form.transferType };
  }

  return payload;
}

// Mirror of backend `identifier_format.bic_problem` (ISO 9362). The backend is the source
// of truth; this copy only makes the error *reactive* — it clears as the user corrects the
// field, instead of a one-shot banner snapshot from a past submit (Doina 2026-09-15). Keep
// the rule byte-close to the Python module. `form.bic` is uppercased on change, so the
// uppercase-only shape check is safe.
const BIC_RE = /^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$/;
function bicProblem(bic) {
  if (!bic) return "";
  if (bic.length !== 8 && bic.length !== 11) {
    return `BIC '${bic}' is ${bic.length} characters; ISO 9362 allows 8 or 11.`;
  }
  if (!BIC_RE.test(bic)) {
    return "BIC is not ISO 9362 shaped (4 letters, 2-letter country, 2 alphanumeric, optional 3-character branch).";
  }
  return "";
}

function validate(form) {
  const e = {};
  if (!form.customerId) e.customerId = "Select a customer.";
  if (!form.debtorAccountId) e.debtorAccountId = "Select the account to debit.";

  const amount = Number(form.amount);
  if (!form.amount) e.amount = "Enter an amount.";
  else if (Number.isNaN(amount) || amount <= 0) e.amount = "Must be greater than zero.";
  if (!/^[A-Za-z]{3}$/.test(form.currency)) e.currency = "Use a 3-letter ISO code.";
  if (!form.purpose) e.purpose = "Describe the payment.";

  if (isWire(form)) {
    if (!form.beneficiaryName) e.beneficiaryName = "Required.";
    if (!form.beneficiaryAccountNo) e.beneficiaryAccountNo = "Required.";
    if (!form.beneficiaryCountry) e.beneficiaryCountry = "Required.";
    // The spec's validator requires a creditor BIC on WIRE when the creditor is external.
    if (!form.bic) e.bic = "Required for an external wire.";
    else {
      const p = bicProblem(form.bic);
      if (p) e.bic = p;
    }
    if (!form.beneficiaryBankName)
      e.beneficiaryBankName = "Required.";
  } else {
    if (!form.creditorAccountId) e.creditorAccountId = "Select a recipient account.";
    else if (form.creditorAccountId === form.debtorAccountId) {
      e.creditorAccountId = "Must differ from the debit account.";
    }
  }
  return e;
}

// --- demo autofill ---------------------------------------------------------
//
// Fills the form with a plausible payment so the lifecycle can be driven without typing.
// The debit side is never invented: customer, debit account and currency come from real
// `accounts` documents, because `capture` resolves them server-side and rejects anything
// fabricated. Only the external wire beneficiary is made up — which is precisely the half
// the bank does not hold and cannot look up.
//
// Amounts stay at or under 20,000, the RETAIL per-payment entitlement, which is what actually
// decides now: stage 2 checks the amount against the debtor customer's segment policy
// (`entitlement_policy.py`), and the global `PAYMENT_LIMIT_USD` is only a
// malformed-input bound. Autofill picks a real (mostly RETAIL) seed customer, so staying
// under the RETAIL ceiling keeps every autopopulated payment acceptable.

// Autofill's beneficiary-bank pool. Every row here MUST match a `BIC_DIRECTORY` row in
// backend/data/seed/leafy_bank_bian.correspondentBanks.json — `clearingSystemMemberId`
// included.
//
// Why the member id matters (fixed 2026-08-31): autofill used to generate a random 9-digit
// number here, so stage-3 enrichment resolved the directory row and *corrected* it. Every
// autopopulated wire then showed a spurious `from -> to` row in the progressive-enrichment
// panel, which is the one screen the audience is meant to trust. A demo cannot distinguish a
// real enrichment from a fabricated correction, so the pool carries the real values and the
// diff only shows fields that genuinely started empty.
//
// `test_autofill_pool_matches_the_bank_directory` (backend, test_reference_data.py) parses
// this array and asserts it against the seed. Add a bank in one place, add it in both.
const EXTERNAL_BANKS = [
  { bankName: "JPMorgan Chase Bank, N.A.", bic: "CHASUS33", country: "US", clearingSystemCode: "USABA", clearingSystemMemberId: "121000248" },
  { bankName: "Citibank, N.A.", bic: "CITIUS33", country: "US", clearingSystemCode: "USABA", clearingSystemMemberId: "021000089" },
  { bankName: "Barclays Bank PLC", bic: "BARCGB22", country: "GB", clearingSystemCode: "GBDSC", clearingSystemMemberId: "202053" },
  { bankName: "Deutsche Bank AG", bic: "DEUTDEFF", country: "DE", clearingSystemCode: "DEBLZ", clearingSystemMemberId: "50070010" },
  { bankName: "UBS Switzerland AG", bic: "UBSWCHZH", country: "CH", clearingSystemCode: "CHBCC", clearingSystemMemberId: "230" },
  { bankName: "Royal Bank of Canada", bic: "ROYCCAT2", country: "CA", clearingSystemCode: "CACPA", clearingSystemMemberId: "000300002" },
];

// Defined demo scenarios, applied in order: each Autopopulate click fills the next one, and
// the ⓘ on the button explains the payment now in the form in plain words — `title` and
// `story` say what the transaction is, `path` how it travels. 4 international, 4 domestic,
// 4 internal. Everything that decides the path is fixed per scenario (rail, bank, country,
// priority, transfer type, amount); only the debit account (a real funded account) and the
// references are picked at run time.
//
// The `path` copy follows backend routing.py: beneficiary country ≠ US → SWIFT, and USD
// goes through CHASUS33 (JPMorgan Chase) first. Every directory bank is also our
// correspondent for its country, so no extra intermediary bank appears. Domestic: priority
// URGENT/HIGH → Fedwire (settles at once), otherwise CHIPS (netted, cheaper).
const AUTOFILL_SCENARIOS = [
  { group: "International", label: "GB · Barclays", title: "International wire to the UK",
    story: "A supplier payment to Northwind Traders in London, banking with Barclays.",
    path: "Sent over SWIFT via JPMorgan Chase (our US dollar correspondent) to Barclays. Tracked hop by hop with SWIFT gpi.",
    rail: "WIRE", bic: "BARCGB22", payee: "Northwind Traders Ltd", address: "12 Cheapside, London",
    amount: "12450.00", priority: "NORMAL", purpose: "Supplier payment" },
  { group: "International", label: "DE · Deutsche Bank", title: "International wire to Germany",
    story: "A freight bill paid to Fabrikam Logistics in Hamburg, banking with Deutsche Bank. High priority.",
    path: "Sent over SWIFT via JPMorgan Chase to Deutsche Bank. Tracked hop by hop with SWIFT gpi.",
    rail: "WIRE", bic: "DEUTDEFF", payee: "Fabrikam Logistics GmbH", address: "Hafenstrasse 8, Hamburg",
    amount: "8720.50", priority: "HIGH", purpose: "Freight and handling" },
  { group: "International", label: "CH · UBS", title: "International wire to Switzerland",
    story: "A consulting fee paid to Tailspin Aviation in Geneva, banking with UBS.",
    path: "Sent over SWIFT via JPMorgan Chase to UBS. Tracked hop by hop with SWIFT gpi.",
    rail: "WIRE", bic: "UBSWCHZH", payee: "Tailspin Aviation SA", address: "Route de Meyrin 21, Geneva",
    amount: "15300.00", priority: "NORMAL", purpose: "Consulting services" },
  { group: "International", label: "CA · Royal Bank of Canada", title: "International wire to Canada",
    story: "An urgent invoice payment to Adventure Works in Toronto, banking with Royal Bank of Canada.",
    path: "Sent over SWIFT via JPMorgan Chase to Royal Bank of Canada. Tracked hop by hop with SWIFT gpi.",
    rail: "WIRE", bic: "ROYCCAT2", payee: "Adventure Works Supply Co", address: "77 Harbour Street, Toronto",
    amount: "2378.40", priority: "URGENT", purpose: "Invoice settlement" },

  { group: "Domestic", label: "Citibank · urgent", title: "Urgent US wire",
    story: "An urgent supplier payment to Contoso Manufacturing in Detroit, banking with Citibank.",
    path: "Stays in the US. Urgent, so it goes over Fedwire and settles immediately.",
    rail: "WIRE", bic: "CITIUS33", payee: "Contoso Manufacturing Inc", address: "440 Industrial Way, Detroit",
    amount: "5600.00", priority: "URGENT", purpose: "Supplier payment" },
  { group: "Domestic", label: "Citibank · normal", title: "Standard US wire",
    story: "Quarterly rent paid to Wide World Importers in New York, banking with Citibank.",
    path: "Stays in the US. Not urgent, so it goes over CHIPS, which nets payments and costs less than Fedwire.",
    rail: "WIRE", bic: "CITIUS33", payee: "Wide World Importers", address: "350 Fifth Avenue, New York",
    amount: "3180.75", priority: "NORMAL", purpose: "Quarterly rent" },
  { group: "Domestic", label: "Chase · high", title: "High-priority US wire",
    story: "A consulting fee paid to Litware in San Francisco, banking with JPMorgan Chase.",
    path: "Stays in the US. High priority, so it goes over Fedwire and settles immediately.",
    rail: "WIRE", bic: "CHASUS33", payee: "Litware Inc", address: "1 Market Street, San Francisco",
    amount: "9875.00", priority: "HIGH", purpose: "Consulting services" },
  { group: "Domestic", label: "Chase · normal", title: "Standard US wire",
    story: "An invoice paid to Proseware in Chicago, banking with JPMorgan Chase.",
    path: "Stays in the US. Not urgent, so it goes over CHIPS, which nets payments and costs less than Fedwire.",
    rail: "WIRE", bic: "CHASUS33", payee: "Proseware Ltd", address: "200 Park Avenue, Chicago",
    amount: "1240.00", priority: "NORMAL", purpose: "Invoice settlement" },

  { group: "Internal", label: "Third party · small", title: "Transfer to another Leafy Bank customer",
    story: "A small invoice payment to a different customer who also banks with Leafy Bank.",
    path: "Never leaves the bank: both accounts are on our own ledger, so it settles at once.",
    rail: "INTERNAL", transferType: "THIRD_PARTY", amount: "750.00", priority: "NORMAL", purpose: "Invoice settlement" },
  { group: "Internal", label: "Third party · large", title: "Large transfer to another Leafy Bank customer",
    story: "A large supplier payment to a different customer who also banks with Leafy Bank.",
    path: "Never leaves the bank: both accounts are on our own ledger, so it settles at once.",
    rail: "INTERNAL", transferType: "THIRD_PARTY", amount: "14200.00", priority: "HIGH", purpose: "Supplier payment" },
  { group: "Internal", label: "Own account · small", title: "Move money between your own accounts",
    story: "A customer tops up one of their own accounts from another.",
    path: "Never leaves the bank: same customer, same ledger, settles at once.",
    rail: "INTERNAL", transferType: "OWN_ACCOUNT", amount: "1500.00", priority: "NORMAL", purpose: "Savings top-up" },
  { group: "Internal", label: "Own account · large", title: "Large move between your own accounts",
    story: "A treasury sweep: a customer moves a large balance between two of their own accounts.",
    path: "Never leaves the bank: same customer, same ledger, settles at once.",
    rail: "INTERNAL", transferType: "OWN_ACCOUNT", amount: "9800.00", priority: "NORMAL", purpose: "Treasury sweep" },
];


const pick = (xs) => xs[Math.floor(Math.random() * xs.length)];
const digits = (n) =>
  Array.from({ length: n }, () => Math.floor(Math.random() * 10)).join("");

// Mirrors stage 2 (authenticate.py `_UNUSABLE_ACCOUNT_STATES` + entitlement_policy
// `blocking_restrictions`): autofill must never pick an account the backend will refuse, or
// a scenario meant to show routing dies at Authentication & Authorization instead.
const UNUSABLE_ACCOUNT_STATES = new Set(["CLOSED", "DORMANT", "FROZEN"]);
function usable(account, side) {
  if (UNUSABLE_ACCOUNT_STATES.has(account.status)) return false;
  const now = new Date().toISOString();
  const blocking = new Set(["FULL_BLOCK", "LEGAL_HOLD", `${side}_BLOCK`]);
  return !(account.restrictions || []).some(
    (r) => r && blocking.has(r.type) && !(r.expiresAt && String(r.expiresAt) <= now)
  );
}

// Real debit sides only. Prefers an account that passes `fits` AND covers the amount; then
// one that passes `fits`; then any usable one — so the button always fills the form and any
// resulting error is the honest one (balance, currency) rather than a silent no-op.
function pickDebtor(accountsByCustomer, amount, fits) {
  const all = [];
  for (const [customerId, accounts] of accountsByCustomer) {
    for (const account of accounts) {
      if (usable(account, "DEBIT")) all.push({ customerId, account });
    }
  }
  const fitting = all.filter((d) => fits(d));
  const funded = fitting.filter((d) => (d.account.balance?.available ?? 0) >= amount);
  const pool = funded.length ? funded : fitting.length ? fitting : all;
  return pool.length ? pick(pool) : null;
}

function autofill(form, scenario, { accountsByCustomer, allAccounts }) {
  const sameCurrency = (a, b) => (a.currency || "USD") === (b.currency || "USD");
  const creditable = (o) => usable(o, "CREDIT");
  const ownSiblings = ({ customerId, account }) =>
    (accountsByCustomer.get(customerId) || []).filter(
      (o) => o.accountId !== account.accountId && sameCurrency(o, account) && creditable(o)
    );
  const otherCustomers = ({ customerId, account }) =>
    allAccounts.filter(
      (o) => o.customerId !== customerId && sameCurrency(o, account) && creditable(o)
    );

  const isOwn = scenario.transferType === "OWN_ACCOUNT";
  const fits =
    scenario.rail === "WIRE"
      ? ({ account }) => (account.currency || "USD") === "USD"
      : (d) => (isOwn ? ownSiblings(d) : otherCustomers(d)).length > 0;

  const debtor = pickDebtor(accountsByCustomer, Number(scenario.amount), fits);
  if (!debtor) return form;

  const next = {
    ...EMPTY,
    rail: scenario.rail,
    customerId: debtor.customerId,
    debtorAccountId: debtor.account.accountId,
    currency: debtor.account.currency || "USD",
    amount: scenario.amount,
    purpose: scenario.purpose,
    endToEndReference: `E2E-${digits(8)}`,
    clientReference: `INV-${digits(6)}`,
    // Coherent with Demo Controls: when the correspondent is set to take a $25 fee, charges
    // are DEBT so the bank absorbs it and resolution shows the Dr 5214 / Cr 1111 adjustment
    // (reconciliation plan, Decision 2).
    chargeBearer: scenario.rail === "WIRE" && feeTakenInFlight(form) ? "DEBT" : "SLEV",
    priority: scenario.priority,
    // B3: preserve the operator's settlement-outcome selection. autofill rebuilds from
    // EMPTY (which defaults this to MATCHED), so without carrying the current value through,
    // picking Unmatched then Autopopulate silently reverts to MATCHED → the wire settles
    // happy-path and the exception queue never fires (defect `autofill-incoherence` class).
    simulatedSettlementOutcome: form.simulatedSettlementOutcome,
    simulatedStatementOutcome: form.simulatedStatementOutcome,
  };

  if (scenario.rail === "WIRE") {
    const bank = EXTERNAL_BANKS.find((b) => b.bic === scenario.bic);
    return {
      ...next,
      beneficiaryName: scenario.payee,
      beneficiaryAddress: scenario.address,
      beneficiaryAccountNo: digits(10),
      beneficiaryCountry: bank.country,
      beneficiaryBankName: bank.bankName,
      bic: bank.bic,
      clearingSystemCode: bank.clearingSystemCode,
      // The directory's real value, NOT a generated one — see the note on EXTERNAL_BANKS.
      clearingSystemMemberId: bank.clearingSystemMemberId,
      accountNumberType: "Current",
    };
  }

  // Internal: another account we hold, same currency, never the debit account (the rule
  // `validate` enforces). Own-account stays within the customer; third-party leaves it.
  const candidates = isOwn ? ownSiblings(debtor) : otherCustomers(debtor);
  return {
    ...next,
    creditorAccountId: candidates.length ? pick(candidates).accountId : "",
    transferType: scenario.transferType,
  };
}

function SectionCard({ n, title, subtitle, children, className }) {
  return (
    <div className={`${styles.sectionCard} ${className || ""}`}>
      <div className={styles.sectionHeading}>
        <span className={styles.sectionNum}>{n}</span>
        <span>{title}</span>
      </div>
      {subtitle && <div className={styles.sectionSub}>{subtitle}</div>}
      {children}
    </div>
  );
}

export default function InitiateWizard({ onInitiated }) {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState(EMPTY);
  const [showErrors, setShowErrors] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showDemoControls, setShowDemoControls] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const submitKey = useSubmitKey();
  const [submitError, setSubmitError] = useState(null);
  const [createdId, setCreatedId] = useState(null);
  // 2026-09-09 (Kiran): a held payment is approved in the lifecycle at stage 2, NOT here. Set
  // when Initiate returns the payment HELD for a second factor, so the confirmation routes the
  // user to the lifecycle to complete it rather than popping the OTP dialog over this form.
  const [awaitingStepUp, setAwaitingStepUp] = useState(false);
  // Autopopulate walks AUTOFILL_SCENARIOS in order; the index survives "Clear form" so the
  // presenter keeps their place in the cycle.
  const [scenarioIdx, setScenarioIdx] = useState(0);
  const [appliedScenario, setAppliedScenario] = useState(null);
  const { customers, accountsByCustomer, allAccounts, loading } = useBankAssistedParties();

  const set = (k, v) => {
    setForm((f) => ({ ...f, [k]: v }));
    // A field edit dismisses a stale backend error. `submitError` is a snapshot of a past
    // submit; edit it away so correcting a value clears the banner instead of leaving the
    // old message up (Doina 2026-09-15 — BIC 'TEST3' persisted after correction).
    setSubmitError(null);
  };
  const errors = useMemo(() => validate(form), [form]);
  const err = (k) => (showErrors ? errors[k] : undefined);
  const state = (k) => (err(k) ? "error" : "none");

  const customer = customers.find((c) => c.customerId === form.customerId) || null;
  const customerAccounts = accountsByCustomer.get(form.customerId) || [];
  const debtor = customerAccounts.find((a) => a.accountId === form.debtorAccountId) || null;
  const selectedType = PAYMENT_TYPES.find((t) => t.rail === form.rail);

  const recipientLabel = isWire(form)
    ? form.beneficiaryName || "—"
    : (() => {
        const a = allAccounts.find((x) => x.accountId === form.creditorAccountId);
        return a ? `${a.ownerName || ""} ${a.type} ····${String(a.accountNumber || "").slice(-4)}`.trim() : "—";
      })();

  function toReview() {
    if (Object.keys(errors).length) {
      setShowErrors(true);
      return;
    }
    setShowErrors(false);
    setStep(1);
  }

  function autopopulate() {
    const scenario = AUTOFILL_SCENARIOS[scenarioIdx];
    setForm((f) => autofill(f, scenario, { accountsByCustomer, allAccounts }));
    setAppliedScenario(scenario);
    setScenarioIdx((i) => (i + 1) % AUTOFILL_SCENARIOS.length);
    setShowErrors(false);
    setSubmitError(null);
  }

  function reset() {
    setForm(EMPTY);
    setAppliedScenario(null);
    setStep(0);
    setShowErrors(false);
    setSubmitError(null);
    setCreatedId(null);
    setAwaitingStepUp(false);
  }

  async function submit() {
    setSubmitting(true);
    setSubmitError(null);
    const { data, error } = await coreApi("PaymentOrderInitiation/Initiate", {
      method: "POST",
      body: buildPayload(form),
      idempotencyKey: submitKey.key(),
    });
    submitKey.settle(error);
    setSubmitting(false);
    if (error) {
      setSubmitError(error);
      return;
    }
    // Stage 2 HELD the payment for a second factor. Per Kiran (2026-09-09) it is approved in
    // the lifecycle at stage 2, not over this form — so surface a made-and-awaiting-verification
    // confirmation and route the user there (View lifecycle), one payment, one id.
    if (data?.stepUpRequired) {
      setAwaitingStepUp(true);
      setCreatedId(data?.paymentId || data?.payment_id || data?.id || null);
      setStep(2);
      return;
    }
    setCreatedId(data?.paymentId || data?.payment_id || data?.id || null);
    setStep(2);
  }

  // --- confirmation ---------------------------------------------------------

  if (step === 2) {
    return (
      <div className={styles.panel}>
        <div className={styles.panelBody}>
          <div className={styles.stepperBar}>
            <Stepper currentStep={2} maxDisplayedSteps={3}>
              {STEPS.map((l) => <Step key={l}>{l}</Step>)}
            </Stepper>
          </div>
          <div className={styles.confirmBox}>
            <Icon
              glyph={awaitingStepUp ? "Lock" : "CheckmarkWithCircle"}
              size={48}
              fill={awaitingStepUp ? "#00684a" : "#00684a"}
            />
            <H2>{awaitingStepUp ? "Payment created" : "Payment submitted"}</H2>
            <Body className={styles.muted}>
              {awaitingStepUp
                ? "This payment is awaiting an additional authentication step (a second factor) at stage 2. Open the lifecycle to approve it and continue."
                : "The payment order was accepted and is moving through the lifecycle."}
            </Body>
            <div className={styles.confirmId}>{createdId || "—"}</div>
            <div className={styles.headerActions}>
              <Button onClick={reset}>Create another</Button>
              <Button
                variant="primary"
                disabled={!createdId}
                rightGlyph={<Icon glyph="ArrowRight" />}
                onClick={() => onInitiated?.(createdId)}
              >
                {awaitingStepUp ? "Approve in lifecycle" : "View lifecycle"}
              </Button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // --- review ---------------------------------------------------------------

  if (step === 1) {
    const rows = [
      ["Payment type", selectedType.label],
      ["Customer", customer?.identification?.legalName || form.customerId],
      ["Debit account", debtor ? `${debtor.type} ····${String(debtor.accountNumber || "").slice(-4)}` : form.debtorAccountId],
      ["Amount", fmtAmount(form.amount, form.currency)],
      ["Beneficiary", recipientLabel],
      ...(isWire(form)
        ? [
            ["Beneficiary account", form.beneficiaryAccountNo],
            ["Beneficiary bank", `${form.beneficiaryBankName} (${form.bic})`],
            ["Country", form.beneficiaryCountry],
            ["Routing / member ID", form.clearingSystemMemberId || "—"],
          ]
        : [["Transfer type", form.transferType]]),
      ["Value date", form.valueDate || "Today"],
      ["Purpose", form.purpose],
      ["End-to-end reference", form.endToEndReference || "—"],
      ["Client reference", form.clientReference || "—"],
      ["Charges", form.chargeBearer],
      ["Priority", form.priority],
      // Stage 7 outcome row — re-enabled for stage 9 (doc 24 §3 step 8).
      ...(isWire(form)
        ? [["Settlement outcome", form.simulatedSettlementOutcome],
           ["Correspondent statement", form.simulatedStatementOutcome]]
        : []),
      ["Channel", "BRANCH (bank-assisted)"],
    ];
    return (
      <div className={styles.panel}>
        {/* Step-up is approved in the lifecycle (stage 2), not over this form (2026-09-09) —
            see the "Approve in lifecycle" confirmation. */}
        <div className={styles.panelBody}>
          <div className={styles.createHeader}>
            <div>
              <H2>Review payment</H2>
              <Body className={styles.subtitle}>
                Confirm the details before submitting for processing.
              </Body>
            </div>
          </div>
          <div className={styles.stepperBar}>
            <Stepper currentStep={1} maxDisplayedSteps={3}>
              {STEPS.map((l) => <Step key={l}>{l}</Step>)}
            </Stepper>
          </div>

          {isWire(form) && (
            <Banner variant="warning">
              Check the beneficiary details carefully. Wire transfers may be difficult or
              impossible to recover after they are sent.
            </Banner>
          )}

          <div className={styles.twoCol} style={{ marginTop: 16 }}>
            <table className={styles.kv}>
              <tbody>
                {rows.slice(0, Math.ceil(rows.length / 2)).map(([k, v]) => (
                  <tr key={k}><td>{k}</td><td>{v || "—"}</td></tr>
                ))}
              </tbody>
            </table>
            <table className={styles.kv}>
              <tbody>
                {rows.slice(Math.ceil(rows.length / 2)).map(([k, v]) => (
                  <tr key={k}><td>{k}</td><td>{v || "—"}</td></tr>
                ))}
              </tbody>
            </table>
          </div>

          {submitError && (
            <div style={{ marginTop: 16 }}>
              <Banner variant="danger">Could not initiate — {submitError}</Banner>
            </div>
          )}

          <div className={styles.formActions}>
            <Button onClick={() => setStep(0)} disabled={submitting} leftGlyph={<Icon glyph="ArrowLeft" />}>
              Back to details
            </Button>
            <div className={styles.formActionsRight}>
              <Button onClick={reset} disabled={submitting}>Cancel</Button>
              <Button variant="primary" onClick={submit} disabled={submitting}>
                {submitting ? "Submitting…" : "Submit payment"}
              </Button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // --- enter details --------------------------------------------------------

  return (
    <div className={styles.panel}>
      <div className={styles.panelBody}>
        <div className={styles.createHeader}>
          <div>
            <H2>Create Payment</H2>
            <Body className={styles.subtitle}>
              Enter payment details and submit for processing.
            </Body>
          </div>
          <div className={styles.headerActions}>
            <Button onClick={reset}>Clear form</Button>
            <Button
              onClick={autopopulate}
              disabled={loading || !allAccounts.length}
              leftGlyph={<Icon glyph="Refresh" />}
            >
              <span className={styles.autofillLabel}>
                Autopopulate
                {/* A span, not a button: a button may not nest in a button. Hover-only, so a
                    click on it still autopopulates like the rest of the button. */}
                <Tooltip
                  trigger={
                    <span className={styles.autofillInfo} aria-label="What was populated">
                      <Icon glyph="InfoWithCircle" size={14} />
                    </span>
                  }
                >
                  {appliedScenario ? (
                    <div className={styles.autofillTip}>
                      <strong>{appliedScenario.title}</strong>
                      <span>
                        {fmtAmount(appliedScenario.amount, form.currency)} · {appliedScenario.story}
                      </span>
                      <span>
                        <b>How it travels:</b> {appliedScenario.path}
                      </span>
                      <span className={styles.autofillTipNote}>
                        Scenario {AUTOFILL_SCENARIOS.indexOf(appliedScenario) + 1} of{" "}
                        {AUTOFILL_SCENARIOS.length}. The paying customer and account are picked
                        at random; click again for the next scenario.
                      </span>
                    </div>
                  ) : (
                    `Fills the form with a ready-made demo payment. Each click moves to the next of ${AUTOFILL_SCENARIOS.length} scenarios: international wires, US wires and internal transfers.`
                  )}
                </Tooltip>
              </span>
            </Button>
            <Button
              variant="primary"
              onClick={toReview}
              disabled={loading}
              rightGlyph={<Icon glyph="ArrowRight" />}
            >
              Review &amp; Submit
            </Button>
          </div>
        </div>

        <div className={styles.stepperBar}>
          <Stepper currentStep={0} maxDisplayedSteps={3}>
            {STEPS.map((l) => <Step key={l}>{l}</Step>)}
          </Stepper>
        </div>

        {loading && <div className={styles.emptyState}>Loading customers and accounts…</div>}

        {!loading && (
          <div className={styles.createGrid}>
            {isWire(form) && (
              <SectionCard
                n={0}
                title="Demo Controls"
                subtitle="Simulation levers for the presenter — not part of the payment instruction."
                className={styles.fullSpan}
              >
                <button
                  type="button"
                  className={styles.disclosure}
                  onClick={() => setShowDemoControls((v) => !v)}
                  aria-expanded={showDemoControls}
                >
                  <Icon glyph={showDemoControls ? "ChevronUp" : "ChevronDown"} size={12} />
                  {showDemoControls ? "Hide demo controls" : "Show demo controls"}
                </button>
                {showDemoControls && (
                  <div className={styles.twoCol}>
                    <div className={styles.fieldStack}>
                      {/* Stage 9 — settlement-outcome selector, re-enabled for the exceptions
                          demo (doc 24 §3 step 8). Was commented out 2026-09-22 (stage 7's enum ask
                          was schema, not driver); stage 9 needs the non-happy paths reachable to
                          populate the exception queue. Backend plumbing stayed in place. */}
                      <Select
                        label="Simulated settlement outcome"
                        description="Stage 7 demo control — picks the simulated external settlement response."
                        value={form.simulatedSettlementOutcome}
                        onChange={(v) => set("simulatedSettlementOutcome", v)}
                        allowDeselect={false}
                      >
                        {SETTLEMENT_OUTCOMES.map((o) => (
                          <Option key={o.value} value={o.value} description={o.description}>
                            {o.label}
                          </Option>
                        ))}
                      </Select>
                      {form.simulatedSettlementOutcome !== "MATCHED" && (
                        <div className={styles.infoBox}>
                          This forces a non-happy-path settlement: the payment will{" "}
                          {form.simulatedSettlementOutcome === "DELAYED"
                            ? "hold at IN_PROGRESS (settlement PENDING)"
                            : form.simulatedSettlementOutcome === "UNMATCHED"
                              ? "settle, then appear $25 short on the correspondent's statement — reconciliation flags the gap for the AI agent and the exception queue"
                              : "RETURN (network/correspondent rejection)"}{" "}
                          — for demoing the distinct downstream outcomes (FR-7.3).
                        </div>
                      )}
                    </div>
                    <div className={styles.fieldStack}>
                      <Select
                        label="Simulated correspondent statement"
                        description="How the correspondent's end-of-day camt.053 books this wire."
                        value={form.simulatedStatementOutcome}
                        onChange={(v) => set("simulatedStatementOutcome", v)}
                        allowDeselect={false}
                      >
                        {STATEMENT_OUTCOMES.map((o) => (
                          <Option key={o.value} value={o.value} description={o.description}>
                            {o.label}
                          </Option>
                        ))}
                      </Select>
                    </div>
                  </div>
                )}
              </SectionCard>
            )}

            <SectionCard n={1} title="Select Payment Type" className={styles.fullSpan}>
              <div style={{ maxWidth: 420 }}>
                <Select
                  label="Payment Type"
                  value={form.rail}
                  onChange={(v) => set("rail", v)}
                  allowDeselect={false}
                >
                  {PAYMENT_TYPES.map((t) => (
                    <Option
                      key={t.rail}
                      value={t.rail}
                      disabled={!t.available}
                      glyph={<Icon glyph={t.glyph} />}
                      description={t.available ? "Available" : "Coming soon"}
                    >
                      {t.label}
                    </Option>
                  ))}
                </Select>
              </div>
            </SectionCard>

            <SectionCard
              n={2}
              title="Payment Details (Canonical)"
              subtitle="Common-layer fields — identical on every rail."
            >
              <div className={styles.twoCol}>
                <div className={styles.fieldStack}>
                  <div className={styles.colHeading}>Debit side</div>
                  <Select
                    label="Customer"
                    description="Who this payment is created for."
                    placeholder="Select a customer"
                    value={form.customerId}
                    onChange={(v) => setForm((f) => ({ ...f, customerId: v, debtorAccountId: "" }))}
                    errorMessage={err("customerId")}
                    state={state("customerId")}
                    allowDeselect={false}
                  >
                    {customers.map((c) => (
                      <Option key={c.customerId} value={c.customerId}>
                        {c.identification?.legalName || c.customerId}
                      </Option>
                    ))}
                  </Select>

                  <Select
                    label="Debit Account"
                    placeholder={form.customerId ? "Select an account" : "Select a customer first"}
                    value={form.debtorAccountId}
                    onChange={(v) => set("debtorAccountId", v)}
                    disabled={!form.customerId}
                    errorMessage={err("debtorAccountId")}
                    state={state("debtorAccountId")}
                    allowDeselect={false}
                  >
                    {customerAccounts.map((a) => (
                      <Option key={a.accountId} value={a.accountId}>
                        {`${a.type} | ····${String(a.accountNumber || "").slice(-4)} | ${a.currency || "USD"}`}
                      </Option>
                    ))}
                  </Select>
                  {debtor?.balance?.available != null && (
                    <div className={styles.balanceHint}>
                      Available balance: {fmtAmount(debtor.balance.available, debtor.currency)}
                    </div>
                  )}

                  <TextInput
                    label="Payment Currency"
                    description="ISO 4217"
                    value={form.currency}
                    onChange={(e) => set("currency", e.target.value.toUpperCase())}
                    errorMessage={err("currency")}
                    state={state("currency")}
                  />
                  <TextInput
                    label="Payment Amount"
                    type="number"
                    value={form.amount}
                    onChange={(e) => set("amount", e.target.value)}
                    errorMessage={err("amount")}
                    state={state("amount")}
                  />
                  <TextInput
                    label="Value Date"
                    type="date"
                    description="Requested execution date. Defaults to today."
                    optional
                    value={form.valueDate}
                    onChange={(e) => set("valueDate", e.target.value)}
                  />
                </div>

                <div className={styles.fieldStack}>
                  <div className={styles.colHeading}>Beneficiary</div>

                  {isWire(form) ? (
                    <>
                      <TextInput
                        label="Beneficiary Name"
                        value={form.beneficiaryName}
                        onChange={(e) => set("beneficiaryName", e.target.value)}
                        errorMessage={err("beneficiaryName")}
                        state={state("beneficiaryName")}
                      />
                      <TextInput
                        label="Beneficiary Account"
                        value={form.beneficiaryAccountNo}
                        onChange={(e) => set("beneficiaryAccountNo", e.target.value)}
                        errorMessage={err("beneficiaryAccountNo")}
                        state={state("beneficiaryAccountNo")}
                      />
                      <TextInput
                        label="Beneficiary Address"
                        optional
                        value={form.beneficiaryAddress}
                        onChange={(e) => set("beneficiaryAddress", e.target.value)}
                      />
                      <TextInput
                        label="Beneficiary Country"
                        description="2-letter ISO code"
                        value={form.beneficiaryCountry}
                        onChange={(e) => set("beneficiaryCountry", e.target.value.toUpperCase())}
                        errorMessage={err("beneficiaryCountry")}
                        state={state("beneficiaryCountry")}
                      />
                    </>
                  ) : (
                    <>
                      <Select
                        label="Beneficiary Account"
                        description="Any Leafy Bank current or savings account."
                        placeholder="Select a recipient"
                        value={form.creditorAccountId}
                        onChange={(v) => set("creditorAccountId", v)}
                        errorMessage={err("creditorAccountId")}
                        state={state("creditorAccountId")}
                        allowDeselect={false}
                      >
                        {allAccounts
                          .filter((a) => a.accountId !== form.debtorAccountId)
                          .map((a) => (
                            <Option key={a.accountId} value={a.accountId}>
                              {`${a.ownerName ? a.ownerName + " | " : ""}${a.type} | ····${String(a.accountNumber || "").slice(-4)}`}
                            </Option>
                          ))}
                      </Select>
                      <div className={styles.infoBox}>
                        Beneficiary name, address and BIC resolve from the account snapshot
                        server-side — no need to key them.
                      </div>
                    </>
                  )}

                  <TextInput
                    label="Payment Purpose / Description"
                    value={form.purpose}
                    onChange={(e) => set("purpose", e.target.value)}
                    errorMessage={err("purpose")}
                    state={state("purpose")}
                  />
                  <TextInput
                    label="End-to-End Reference"
                    description="Carried unchanged to the beneficiary."
                    optional
                    value={form.endToEndReference}
                    onChange={(e) => set("endToEndReference", e.target.value)}
                  />
                  <TextInput
                    label="Client Reference"
                    description="PO number, invoice ID — your own record."
                    optional
                    value={form.clientReference}
                    onChange={(e) => set("clientReference", e.target.value)}
                  />
                  <Select
                    label="Charges"
                    value={form.chargeBearer}
                    onChange={(v) => set("chargeBearer", v)}
                    allowDeselect={false}
                  >
                    {CHARGE_BEARERS.map(([v, l]) => (
                      <Option key={v} value={v}>{l}</Option>
                    ))}
                  </Select>
                </div>
              </div>
            </SectionCard>

            <SectionCard
              n={3}
              title="Payment Type Details"
              subtitle={selectedType.blurb}
            >
              <div className={styles.fieldStack}>
                <div className={styles.subPanel}>
                  <div className={styles.subPanelTitle}>
                    Additional fields for {selectedType.label}
                  </div>
                  <div className={styles.fieldStack}>
                    {isWire(form) ? (
                      <>
                        <TextInput
                          label="Beneficiary Bank Name"
                          value={form.beneficiaryBankName}
                          onChange={(e) => set("beneficiaryBankName", e.target.value)}
                          errorMessage={err("beneficiaryBankName")}
                          state={state("beneficiaryBankName")}
                        />
                        <TextInput
                          label="SWIFT / BIC Code"
                          value={form.bic}
                          onChange={(e) => set("bic", e.target.value.toUpperCase())}
                          errorMessage={err("bic")}
                          state={state("bic")}
                        />
                        <TextInput
                          label="ABA / Routing Number"
                          description="Clearing system member ID."
                          optional
                          value={form.clearingSystemMemberId}
                          onChange={(e) => set("clearingSystemMemberId", e.target.value)}
                        />
                        <Select
                          label="Clearing System"
                          placeholder="None"
                          value={form.clearingSystemCode}
                          onChange={(v) => set("clearingSystemCode", v)}
                        >
                          {CLEARING_SYSTEMS.map((c) => (
                            <Option key={c} value={c}>{c}</Option>
                          ))}
                        </Select>
                        <Select
                          label="Account Number Type"
                          value={form.accountNumberType}
                          onChange={(v) => set("accountNumberType", v)}
                          allowDeselect={false}
                        >
                          {ACCOUNT_TYPES.map((t) => (
                            <Option key={t} value={t}>{t}</Option>
                          ))}
                        </Select>
                      </>
                    ) : (
                      <Select
                        label="Transfer Type"
                        value={form.transferType}
                        onChange={(v) => set("transferType", v)}
                        allowDeselect={false}
                      >
                        {TRANSFER_TYPES.map(([v, l]) => (
                          <Option key={v} value={v}>{l}</Option>
                        ))}
                      </Select>
                    )}

                    <Select
                      label="Priority"
                      value={form.priority}
                      onChange={(v) => set("priority", v)}
                      allowDeselect={false}
                    >
                      {PRIORITIES.map((p) => (
                        <Option key={p} value={p}>{p}</Option>
                      ))}
                    </Select>

                    {isWire(form) && (
                      <>
                        <button
                          type="button"
                          className={styles.disclosure}
                          onClick={() => setShowAdvanced((v) => !v)}
                          aria-expanded={showAdvanced}
                        >
                          <Icon glyph={showAdvanced ? "ChevronUp" : "ChevronDown"} size={12} />
                          {showAdvanced ? "Hide additional options" : "Show additional options"}
                        </button>
                        {showAdvanced && (
                          <>
                            <TextInput
                              label="Service Level Code"
                              description="pain.001 SvcLvl, e.g. URGP."
                              optional
                              value={form.serviceLevelCode}
                              onChange={(e) => set("serviceLevelCode", e.target.value.toUpperCase())}
                            />
                            <TextInput
                              label="Local Instrument Code"
                              optional
                              value={form.localInstrumentCode}
                              onChange={(e) => set("localInstrumentCode", e.target.value.toUpperCase())}
                            />
                            <div className={styles.infoBox}>
                              Wire type (domestic vs international) is derived server-side from
                              the two bank countries — it is not keyed here.
                            </div>
                          </>
                        )}
                      </>
                    )}
                  </div>
                </div>
              </div>
            </SectionCard>
          </div>
        )}

        {showErrors && Object.keys(errors).length > 0 && (
          <div style={{ marginTop: 16 }}>
            <Banner variant="danger">
              {Object.keys(errors).length} field
              {Object.keys(errors).length === 1 ? "" : "s"} need attention before you can review.
            </Banner>
          </div>
        )}
      </div>
    </div>
  );
}
