"use client";

// Create Payment — bank-assisted initiation (doc 16 §7b).
//
// A rich two-pane surface, not a plain form. The left pane is the form
// (payment type → from/amount/to → details); the right pane is a live order
// summary that recomputes as the form changes (this is the Wise / Stripe /
// Mercury "send money" pattern — the summary is the source of truth for the
// money story, so it stays in view and never contradicts the form).
//
// The section split mirrors the request contract rather than being cosmetic:
//   Payment type        → `rail` (and which envelope the details render)
//   From → Amount → To  → the canonical common layer (debtor, amount, creditor)
//   Payment details     → the rest of the common layer + the rail envelope
//                        (`wireDetails` / `internalDetails`)
//
// Doina's mockup is customer self-service; this is the bank-assisted variant an
// employee drives, which per her own Frontend research differs only at the
// initiation layer — hence the Customer field and `channel: "BRANCH"`.
import { useMemo, useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Banner from "@leafygreen-ui/banner";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import { OptionGroup, Select, Option } from "@leafygreen-ui/select";
import {
  SegmentedControl,
  SegmentedControlOption,
} from "@leafygreen-ui/segmented-control";
import TextInput from "@leafygreen-ui/text-input";
import { Body, H2, Overline } from "@leafygreen-ui/typography";


import styles from "./PaymentsWorkflow.module.css";
import { coreApi } from "@/lib/api/client";
import { useBankAssistedParties } from "@/lib/api/hooks";
import { fmtAmount } from "@/lib/paymentsWorkflow/status";

// Stepper stops at Confirmation. An "Approve" step is deliberately absent: dual approval
// is stage 2 (doc 15 B4) and has neither a threshold nor an approver today, so showing it
// would promise a gate that does not run.
const STEPS = ["Enter details", "Review", "Confirmation"];

// Phase 1 is wires + internal transfers; ACH and cards are Phase 2. The unavailable rails
// are shown as tiles rather than hidden so the demo can point at the roadmap — `disabled`
// keeps them unselectable.
const PAYMENT_TYPES = [
  { rail: "WIRE", label: "Wires", phase: 1, available: true, glyph: "Building",
    blurb: "Between banks, ISO 20022. 1–2 business days." },
  { rail: "INTERNAL", label: "Internal Transfer", phase: 1, available: true, glyph: "Refresh",
    blurb: "Book transfer between two Leafy Bank accounts. Instant." },
  { rail: "ACH", label: "ACH", phase: 2, available: false, glyph: "Menu",
    blurb: "Batch clearing house transfers. Phase 2." },
  { rail: "CARD", label: "Cards", phase: 2, available: false, glyph: "CreditCard",
    blurb: "Card acquiring and issuing rails. Phase 2." },
];

// Contract values are ISO 20022 codes; the labels spell out who actually pays.
const CHARGE_BEARERS = [
  ["DEBT", "Sender pays all charges"],
  ["CRED", "Beneficiary pays all charges"],
  ["SHAR", "Shared between both parties"],
  ["SLEV", "Following service level"],
];

// Per-rail delivery + fee facts for the order summary. Demo constants (the wire fee is the
// stage-6 WIRE_FEE; delivery figures are illustrative, not a backend claim).
const RAIL_FACTS = {
  WIRE: { label: "Wire transfer", delivery: "1–2 business days", fee: 25.0 },
  INTERNAL: { label: "Internal transfer", delivery: "Instant (on-us)", fee: 0 },
};
const WIRE_FEE = 25.0;

const CLEARING_SYSTEMS = ["USABA", "USPID", "GBDSC", "CHBCC", "DEBLZ", "CACPA"];
const ACCOUNT_TYPES = ["Checking", "Savings", "Current", "FixedDeposit"];
const PRIORITIES = ["NORMAL", "HIGH", "URGENT"];

// ISO 20022 ExternalPurpose1Code values, verbatim from the canonical spec enum
// (`backend/data/seed/leafy_bank_bian.purposeCodes.json`). `categoryPurpose` is the
// top-level common-layer field: stage 3 resolves it against the `purposeCodes`
// collection and mirrors it into `remittance.purposeCode`, which is what feeds the
// pacs.008 CtgyPurp and the stage-4 purpose-code AML check through to execution.
const PURPOSE_CODES = [
  ["SALA", "Salary Payment", "Payroll"],
  ["PENS", "Pension Payment", "Payroll"],
  ["TAXS", "Tax Payment", "Government"],
  ["SUPP", "Supplier Payment", "Trade"],
  ["GDDS", "Purchase of Goods", "Trade"],
  ["SCVE", "Purchase of Services", "Trade"],
  ["INTC", "Intra-Company Payment", "Treasury"],
  ["TREA", "Treasury Payment", "Treasury"],
  ["DIVI", "Dividend Payment", "Securities"],
  ["COMM", "Commission Payment", "Fees"],
  ["LOAN", "Loan Disbursement or Repayment", "Lending"],
  ["SECU", "Securities Settlement", "Securities"],
  ["TRAD", "Trade Settlement Payment", "Trade"],
  ["HEDG", "Hedging Payment", "Securities"],
  ["GOVT", "Government Payment", "Government"],
];

// Grouped by the spec's demo-only `category` field so the Select renders optgroups.
const PURPOSE_CATEGORIES = [...new Set(PURPOSE_CODES.map(([, , cat]) => cat))].map(
  (cat) => ({ label: cat, options: PURPOSE_CODES.filter(([, , c]) => c === cat) })
);

const QUICK_AMOUNTS = [100, 500, 1000, 2500];

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
  categoryPurpose: "",
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
};

const isWire = (form) => form.rail === "WIRE";

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

  // Top-level common-layer purpose code (ISO ExternalPurpose). Optional. The contract
  // accepts it; stage 3 resolves it against `purposeCodes` and mirrors it into
  // `remittance.purposeCode` (`enrichment_plan._plan_purpose_codes`).
  if (form.categoryPurpose) payload.categoryPurpose = form.categoryPurpose;

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

    // `wireType` is deliberately NOT sent — the server derives it from the two bank
    // countries and nulls it when either is unknown (doc 13, initiation_envelope.py).
    const wire = {};
    // serviceLevel / localInstrument nest under paymentTypeInformation — the contract's
    // `WireDetailsBody` has no top-level fields for them (`extra="forbid"`), so sending
    // them flat would 422. Mirror the shape the envelope builder reads.
    const pti = {};
    if (form.serviceLevelCode) pti.serviceLevel = { code: form.serviceLevelCode };
    if (form.localInstrumentCode) pti.localInstrument = { code: form.localInstrumentCode };
    if (Object.keys(pti).length) wire.paymentTypeInformation = pti;
    if (Object.keys(wire).length) payload.wireDetails = wire;
  } else {
    // An account we hold — name, BIC and address resolve from the snapshot server-side.
    payload.creditor = { accountId: form.creditorAccountId };
    // `transferType` is NOT sent. It is DERIVED server-side from the debtor/creditor
    // snapshot (`initiation_envelope.build_internal_details`, feeding off ctx.is_internal
    // = "same customer"), so it is the single source of truth. Sending it would override
    // the derivation and permit an inconsistent OWN_ACCOUNT for a different-customer
    // transfer — the wizard only displays the derived value, never chooses it.
    payload.internalDetails = {};
  }

  return payload;
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
    if (!form.beneficiaryBankName) e.beneficiaryBankName = "Required.";
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
// Amounts stay under the RETAIL per-payment entitlement, which is what actually decides
// now: stage 2 checks the amount against the debtor customer's segment policy
// (`entitlement_policy.py`), and the global `PAYMENT_LIMIT_USD` is only a
// malformed-input bound. Autofill picks a real (mostly RETAIL) seed customer, so staying
// under the RETAIL ceiling keeps every autopopulated payment acceptable.
const AUTOFILL_MIN = 10;
const AUTOFILL_MAX = 20000;

// Autofill's beneficiary pool. Each row is a FULLY COHERENT payee: the address country, the
// beneficiary's bank country, the clearing system and the purpose all agree, so an
// autopopulated wire reads as one believable transaction — not a random splice of a name, a
// bank and an unrelated purpose. The bank facts (bic/country/clearingSystemCode/
// clearingSystemMemberId) are verbatim from the seed directory.
//
// Why the member id matters (fixed 2026-08-31): autofill used to generate a random 9-digit
// number here, so stage-3 enrichment resolved the directory row and *corrected* it. Every
// autopopulated wire then showed a spurious `from -> to` row in the progressive-enrichment
// panel, which is the one screen the audience is meant to trust. A demo cannot distinguish a
// real enrichment from a fabricated correction, so the pool carries the real values and the
// diff only shows fields that genuinely started empty.
//
// `test_autofill_pool_matches_the_bank_directory` (backend, test_reference_data.py) parses
// this array and asserts the bank facts against the seed. Add a bank in one place, add it in
// both.
const EXTERNAL_RECIPIENTS = [
  { bankName: "JPMorgan Chase Bank, N.A.", bic: "CHASUS33", country: "US", clearingSystemCode: "USABA", clearingSystemMemberId: "121000248", payeeName: "Contoso Manufacturing Inc", address: "440 Industrial Way, Detroit, MI 48201", accountNo: "0123456789", accountType: "Checking", purposeText: "Supplier payment", purposeCode: "SUPP" },
  { bankName: "Citibank, N.A.", bic: "CITIUS33", country: "US", clearingSystemCode: "USABA", clearingSystemMemberId: "021000089", payeeName: "Adventure Works Supply Co", address: "782 Market Street, San Francisco, CA 94103", accountNo: "9876543210", accountType: "Checking", purposeText: "Purchase of goods", purposeCode: "GDDS" },
  { bankName: "Barclays Bank PLC", bic: "BARCGB22", country: "GB", clearingSystemCode: "GBDSC", clearingSystemMemberId: "202053", payeeName: "Northwind Traders Ltd", address: "12 Cheapside, London EC2V 6AD", accountNo: "11223344556", accountType: "Current", purposeText: "Invoice settlement", purposeCode: "SUPP" },
  { bankName: "Deutsche Bank AG", bic: "DEUTDEFF", country: "DE", clearingSystemCode: "DEBLZ", clearingSystemMemberId: "50070010", payeeName: "Fabrikam Logistics GmbH", address: "Hafenstrasse 8, 20359 Hamburg", accountNo: "1234567890", accountType: "Current", purposeText: "Freight and handling", purposeCode: "GDDS" },
  { bankName: "UBS Switzerland AG", bic: "UBSWCHZH", country: "CH", clearingSystemCode: "CHBCC", clearingSystemMemberId: "230", payeeName: "Tailspin Aviation SA", address: "Route de Meyrin 21, 1215 Geneva", accountNo: "0201234567", accountType: "Current", purposeText: "Purchase of services", purposeCode: "SCVE" },
  { bankName: "Royal Bank of Canada", bic: "ROYCCAT2", country: "CA", clearingSystemCode: "CACPA", clearingSystemMemberId: "000300002", payeeName: "Lakeshore Foods Ltd", address: "77 Harbour Street, Toronto, ON M5J 0A7", accountNo: "003003497", accountType: "Checking", purposeText: "Supplier payment", purposeCode: "SUPP" },
];

// [free-text purpose, ISO ExternalPurpose code]. The code pairs with the text so an
// autopopulated payment resolves to a real purpose code in stage 3 (PASS, not SKIP).
const AUTOFILL_PURPOSES = [
  ["Invoice settlement", "SUPP"],
  ["Supplier payment", "SUPP"],
  ["Consulting services", "SCVE"],
  ["Quarterly rent", "SCVE"],
  ["Freight and handling", "GDDS"],
];

const pick = (xs) => xs[Math.floor(Math.random() * xs.length)];
const digits = (n) =>
  Array.from({ length: n }, () => Math.floor(Math.random() * 10)).join("");

// Real debit sides only — an account whose available balance covers the amount, so the
// payment clears validation rather than 400-ing on insufficient funds.
function pickDebtor(accountsByCustomer, amount) {
  const eligible = [];
  for (const [customerId, accounts] of accountsByCustomer) {
    for (const a of accounts) {
      if ((a.balance?.available ?? 0) >= amount) eligible.push({ customerId, account: a });
    }
  }
  // Nothing funded well enough: fall back to any pair so the button still fills the form
  // and the resulting error is the honest one about balances.
  if (!eligible.length) {
    for (const [customerId, accounts] of accountsByCustomer) {
      for (const a of accounts) eligible.push({ customerId, account: a });
    }
  }
  return eligible.length ? pick(eligible) : null;
}

function autofill(form, { accountsByCustomer, allAccounts }) {
  const amount = (
    AUTOFILL_MIN + Math.random() * (AUTOFILL_MAX - AUTOFILL_MIN)
  ).toFixed(2);
  const debtor = pickDebtor(accountsByCustomer, Number(amount));
  if (!debtor) return form;
  const purpose = pick(AUTOFILL_PURPOSES);

  const next = {
    ...EMPTY,
    rail: form.rail,
    customerId: debtor.customerId,
    debtorAccountId: debtor.account.accountId,
    currency: debtor.account.currency || "USD",
    amount,
    purpose: purpose[0],
    categoryPurpose: purpose[1],
    endToEndReference: `E2E-${digits(8)}`,
    clientReference: `INV-${digits(6)}`,
    chargeBearer: pick(CHARGE_BEARERS)[0],
    priority: pick(PRIORITIES),
  };

  if (isWire(next)) {
    const rec = pick(EXTERNAL_RECIPIENTS);
    return {
      ...next,
      beneficiaryName: rec.payeeName,
      beneficiaryAddress: rec.address,
      beneficiaryAccountNo: rec.accountNo,
      beneficiaryCountry: rec.country,
      beneficiaryBankName: rec.bankName,
      bic: rec.bic,
      clearingSystemCode: rec.clearingSystemCode,
      // The directory's real value, NOT `digits(9)` — see the note on EXTERNAL_RECIPIENTS.
      clearingSystemMemberId: rec.clearingSystemMemberId,
      accountNumberType: rec.accountType,
      // The recipient's own purpose — coherent with the business, and it makes stage 3
      // resolve a real purpose code (PASS) rather than SKIP.
      purpose: rec.purposeText,
      categoryPurpose: rec.purposeCode,
    };
  }

  // Internal: the creditor must be another account we hold, and must differ from the
  // debit account — same rule `validate` enforces.
  const candidates = allAccounts.filter(
    (a) => a.accountId !== next.debtorAccountId && a.currency === next.currency
  );
  return {
    ...next,
    creditorAccountId: candidates.length ? pick(candidates).accountId : "",
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

// The stepped "From → To" block: debit side, the amount hero, then the beneficiary.
function TransferCard({ kind, children }) {
  return (
    <div className={styles.flowCard}>
      <div className={styles.flowHeading}>
        <span className={styles.flowBadge}>{kind}</span>
      </div>
      {children}
    </div>
  );
}

// Masked account number with a reveal toggle — the standard "show/hide" affordance. The
// account is shown last-4 by default (sensitive identifier) and the full number on click,
// so the operator can confirm the exact account when it matters without it being on screen
// the whole time.
function AccountMask({ type, number }) {
  const [revealed, setRevealed] = useState(false);
  const prefix = type ? `${type} ` : "";
  const masked = `${prefix}····${String(number || "").slice(-4)}`;
  const shown = `${prefix}${number || "—"}`;
  return (
    <span className={styles.acctMask}>
      <span>{revealed ? shown : masked}</span>
      <button
        type="button"
        className={styles.acctMaskToggle}
        onClick={() => setRevealed((v) => !v)}
        aria-label={revealed ? "Hide account number" : "Show account number"}
        aria-pressed={revealed}
        title={revealed ? "Hide account number" : "Show account number"}
      >
        <Icon glyph={revealed ? "Unlock" : "Lock"} size={14} />
      </button>
    </span>
  );
}

// The live right-rail summary. Pure presentational — reads the same derived values the form
// uses, so it can never disagree with the form. Does not submit; the CTA is in the header.
function OrderSummary({
  form, customer, debtor, recipientLabel, isOwnAccount, selectedType,
}) {
  const facts = RAIL_FACTS[form.rail] || RAIL_FACTS.WIRE;
  const amount = Number(form.amount) || 0;
  const hasAmount = amount > 0;
  // The wire fee only bites once an amount is entered — showing a $25 total on an empty
  // form reads as a bug, not a fee.
  const fee = isWire(form) && hasAmount ? WIRE_FEE : 0;
  const total = amount + fee;
  const purposeName = PURPOSE_CODES.find(([c]) => c === form.categoryPurpose)?.[1] || "";
  const recipientSub = isWire(form)
    ? `${form.beneficiaryBankName ? form.beneficiaryBankName + " · " : ""}${form.beneficiaryAccountNo || ""}`
    : isOwnAccount
      ? "Own account"
      : "Third party";

  return (
    <div className={styles.summaryCard}>
      <div className={styles.summaryHead}>
        <Overline>Order summary</Overline>
        <Badge variant={isWire(form) ? "green" : "blue"}>{selectedType.label}</Badge>
      </div>

      <div className={styles.summaryBlock}>
        <div className={styles.summaryLabel}>From</div>
        <div className={styles.summaryValue}>
          {customer?.identification?.legalName || "Select a customer"}
        </div>
        {debtor && (
          <div className={styles.summarySub}>
            <AccountMask type={debtor.type} number={debtor.accountNumber} />
          </div>
        )}
      </div>

      <div className={styles.summaryBlock}>
        <div className={styles.summaryLabel}>To</div>
        <div className={styles.summaryValue}>{recipientLabel || "Select a beneficiary"}</div>
        {recipientLabel && (
          <div className={styles.summarySub}>{recipientSub || "—"}</div>
        )}
      </div>

      <div className={styles.summaryDivider} />

      <div className={styles.summaryMoney}>
        <div className={styles.summaryAmount}>
          {fmtAmount(form.amount || 0, form.currency)}
        </div>
        <div className={styles.summaryAmountTag}>{facts.label}</div>
      </div>

      <div className={styles.summaryLine}>
        <span>Service fee</span>
        <span className={styles.summaryLineValue}>{fee ? fmtAmount(fee, form.currency) : "—"}</span>
      </div>
      <div className={styles.summaryLine}>
        <span>Total debited</span>
        <span className={styles.summaryLineValue}>{fmtAmount(total, form.currency)}</span>
      </div>
      <div className={styles.summaryLine}>
        <span>Arrives</span>
        <span className={styles.summaryLineValue}>{facts.delivery}</span>
      </div>

      <div className={styles.summaryDivider} />

      <div className={styles.summaryLine}>
        <span>Purpose</span>
        <span className={styles.summaryLineValue}>
          {form.categoryPurpose ? `${form.categoryPurpose}${purposeName ? " · " + purposeName : ""}` : "—"}
        </span>
      </div>
      <div className={styles.summaryLine}>
        <span>Priority</span>
        <span className={styles.summaryLineValue}>{form.priority}</span>
      </div>

      <div className={styles.summaryCallout}>
        <Icon glyph="InfoWithCircle" size={16} />
        <span>Bank-assisted initiation · {selectedType.phase === 1 ? "Phase 1" : "Phase 2"}</span>
      </div>
    </div>
  );
}

// Compact, left-aligned step indicator for the Review / Confirmation steps. The create step
// deliberately has NO indicator of its own: its section numbers (1 Payment type · 2 Transfer ·
// 3 Payment details) carry structure there, and a second "1 2 3" floating on top was exactly
// the oddity flagged on the first draft.
function StepIndicator({ current }) {
  return (
    <div className={styles.reviewSteps} aria-label={`Step ${current} of 3`}>
      {STEPS.map((label, i) => {
        const n = i + 1;
        const done = n < current;
        const active = n === current;
        return (
          <div key={label} className={styles.reviewStep}>
            <span
              className={[
                styles.reviewStepDot,
                done && styles.reviewStepDotDone,
                active && styles.reviewStepDotCurrent,
              ].filter(Boolean).join(" ")}
            >
              {done ? <Icon glyph="Checkmark" size={12} /> : n}
            </span>
            <span className={[styles.reviewStepLabel, active && styles.reviewStepLabelActive].filter(Boolean).join(" ")}>
              {label}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export default function InitiateWizard({ onInitiated }) {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState(EMPTY);
  const [showErrors, setShowErrors] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  const [createdId, setCreatedId] = useState(null);
  // 2026-09-09 (Kiran): a held payment is approved in the lifecycle at stage 2, NOT here. Set
  // when Initiate returns the payment HELD for a second factor, so the confirmation routes the
  // user to the lifecycle to complete it rather than popping the OTP dialog over this form.
  const [awaitingStepUp, setAwaitingStepUp] = useState(false);

  const { customers, accountsByCustomer, allAccounts, loading } = useBankAssistedParties();

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));
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

  // `internalDetails.transferType` is DERIVED, not chosen — OWN_ACCOUNT when the creditor
  // shares the debtor's customer (FR-1.7; the server derives it from the same rule,
  // `capture.py ctx.is_internal` = "same customer, not same bank"). Compute it here so the
  // read-only display always agrees with what the server persists. The wizard never sends
  // the field (see buildPayload), so a user cannot create an inconsistent OWN_ACCOUNT.
  const creditorAccount = allAccounts.find((x) => x.accountId === form.creditorAccountId);
  const isOwnAccount =
    form.rail === "INTERNAL" &&
    Boolean(form.customerId) &&
    Boolean(creditorAccount) &&
    creditorAccount.customerId === form.customerId;
  const transferTypeLabel =
    form.rail !== "INTERNAL"
      ? ""
      : !form.creditorAccountId
        ? "—"
        : isOwnAccount
          ? "Own account"
          : "Third party";

  // The quick-amount chip that matches the debtor's available balance exactly.
  const chipUseAvailable = debtor?.balance?.available != null;
  const amountOverBalance =
    !!form.amount && !!debtor && Number(form.amount) > (debtor.balance?.available ?? 0);

  function toReview() {
    if (Object.keys(errors).length) {
      setShowErrors(true);
      return;
    }
    setShowErrors(false);
    setStep(1);
  }

  function autopopulate() {
    setForm((f) => autofill(f, { accountsByCustomer, allAccounts }));
    setShowErrors(false);
    setSubmitError(null);
  }

  function reset() {
    setForm(EMPTY);
    setStep(0);
    setShowErrors(false);
    setSubmitError(null);
    setCreatedId(null);
  }

  async function submit() {
    setSubmitting(true);
    setSubmitError(null);
    const { data, error } = await coreApi("PaymentOrderInitiation/Initiate", {
      method: "POST",
      body: buildPayload(form),
    });
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
          <StepIndicator current={3} />
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
    // Read-only render of the SAME two-pane layout the initiate step uses — From / Amount / To
    // cards + the live order-summary rail — so Review reads as a locked preview, not a tabular
    // surprise. The submit actions live at the bottom; the step indicator is left-aligned.
    return (
      <div className={styles.panel}>
        {/* Step-up is approved in the lifecycle (stage 2), not over this form (2026-09-09) —
            see the "Approve in lifecycle" confirmation. */}
        <div className={styles.panelBody}>
          <div className={styles.createHeader}>
            <div>
              <StepIndicator current={2} />
              <H2>Review payment</H2>
              <Body className={styles.subtitle}>
                Confirm the details before submitting for processing.
              </Body>
            </div>
          </div>

          {isWire(form) && (
            <Banner variant="warning">
              Check the beneficiary details carefully. Wire transfers may be difficult or
              impossible to recover after they are sent.
            </Banner>
          )}

          <div className={styles.initiateGrid}>
            <div className={styles.initiateMain}>
              <SectionCard n={2} title="Transfer">
                <div className={styles.transferPath}>
                  <TransferCard kind="From">
                    <div className={styles.kvBlock}>
                      <div className={styles.kvRow}>
                        <span className={styles.kvRowLabel}>Customer</span>
                        <span className={styles.kvRowValue}>{customer?.identification?.legalName || form.customerId}</span>
                      </div>
                      <div className={styles.kvRow}>
                        <span className={styles.kvRowLabel}>Debit account</span>
                        <span className={styles.kvRowValue}>
                          {debtor ? <AccountMask type={debtor.type} number={debtor.accountNumber} /> : form.debtorAccountId}
                        </span>
                      </div>
                      {debtor?.balance?.available != null && (
                        <div className={styles.kvRow}>
                          <span className={styles.kvRowLabel}>Available</span>
                          <span className={styles.kvRowValue}>{fmtAmount(debtor.balance.available, debtor.currency)}</span>
                        </div>
                      )}
                    </div>
                  </TransferCard>

                  <div className={styles.amountHero}>
                    <div className={styles.amountHeroTop}>
                      <span className={styles.amountHeroLabel}>Amount</span>
                      <span className={styles.amountCurrency}>{form.currency}</span>
                    </div>
                    <div className={styles.amountReadonly}>{fmtAmount(form.amount, form.currency)}</div>
                    <div className={styles.amountMeta}>
                      {selectedType.label} · {recipientLabel}
                    </div>
                  </div>

                  <TransferCard kind="To">
                    {isWire(form) ? (
                      <div className={styles.kvBlock}>
                        <div className={styles.kvRow}>
                          <span className={styles.kvRowLabel}>Beneficiary</span>
                          <span className={styles.kvRowValue}>{form.beneficiaryName}</span>
                        </div>
                        <div className={styles.kvRow}>
                          <span className={styles.kvRowLabel}>Account</span>
                          <span className={styles.kvRowValue}>{form.beneficiaryAccountNo}</span>
                        </div>
                        <div className={styles.kvRow}>
                          <span className={styles.kvRowLabel}>Bank</span>
                          <span className={styles.kvRowValue}>{form.beneficiaryBankName} ({form.bic})</span>
                        </div>
                        <div className={styles.kvRow}>
                          <span className={styles.kvRowLabel}>Country</span>
                          <span className={styles.kvRowValue}>{form.beneficiaryCountry}</span>
                        </div>
                        <div className={styles.kvRow}>
                          <span className={styles.kvRowLabel}>Routing</span>
                          <span className={styles.kvRowValue}>{form.clearingSystemMemberId || "—"}</span>
                        </div>
                      </div>
                    ) : (
                      <div className={styles.kvBlock}>
                        <div className={styles.kvRow}>
                          <span className={styles.kvRowLabel}>Beneficiary</span>
                          <span className={styles.kvRowValue}>{creditorAccount?.ownerName || "—"}</span>
                        </div>
                        <div className={styles.kvRow}>
                          <span className={styles.kvRowLabel}>Account</span>
                          <span className={styles.kvRowValue}>
                            {creditorAccount
                              ? <AccountMask type={creditorAccount.type} number={creditorAccount.accountNumber} />
                              : "—"}
                          </span>
                        </div>
                        <div className={styles.kvRow}>
                          <span className={styles.kvRowLabel}>Transfer type</span>
                          <span className={styles.kvRowValue}>{transferTypeLabel}</span>
                        </div>
                      </div>
                    )}
                  </TransferCard>
                </div>
              </SectionCard>

              <SectionCard n={3} title="Payment details">
                <div className={styles.kvBlock}>
                  <div className={styles.kvRow}>
                    <span className={styles.kvRowLabel}>Purpose</span>
                    <span className={styles.kvRowValue}>{form.purpose}</span>
                  </div>
                  <div className={styles.kvRow}>
                    <span className={styles.kvRowLabel}>Category purpose</span>
                    <span className={styles.kvRowValue}>{form.categoryPurpose || "—"}</span>
                  </div>
                  <div className={styles.kvRow}>
                    <span className={styles.kvRowLabel}>Value date</span>
                    <span className={styles.kvRowValue}>{form.valueDate || "Today"}</span>
                  </div>
                  <div className={styles.kvRow}>
                    <span className={styles.kvRowLabel}>Priority</span>
                    <span className={styles.kvRowValue}>{form.priority}</span>
                  </div>
                  <div className={styles.kvRow}>
                    <span className={styles.kvRowLabel}>Charges</span>
                    <span className={styles.kvRowValue}>{form.chargeBearer}</span>
                  </div>
                  <div className={styles.kvRow}>
                    <span className={styles.kvRowLabel}>Client reference</span>
                    <span className={styles.kvRowValue}>{form.clientReference || "—"}</span>
                  </div>
                  <div className={styles.kvRow}>
                    <span className={styles.kvRowLabel}>End-to-end reference</span>
                    <span className={styles.kvRowValue}>{form.endToEndReference || "—"}</span>
                  </div>
                </div>
              </SectionCard>
            </div>

            <OrderSummary
              form={form}
              customer={customer}
              debtor={debtor}
              recipientLabel={recipientLabel}
              isOwnAccount={isOwnAccount}
              selectedType={selectedType}
            />
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
              Bank-assisted initiation — set up a wire or internal transfer for a customer.
            </Body>
          </div>
          <div className={styles.headerActions}>
            <Button onClick={reset}>Clear</Button>
            <Button
              onClick={autopopulate}
              disabled={loading || !allAccounts.length}
              leftGlyph={<Icon glyph="Refresh" />}
              title="Fill with a random demo payment on the selected rail"
            >
              Autopopulate
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

        {loading ? (
          <div className={styles.loadingBlock}>
            <Icon glyph="Refresh" size={20} className={styles.spin} />
            <span>Loading customers and accounts…</span>
          </div>
        ) : (
          <div className={styles.initiateGrid}>
            {/* ── form (left) ── */}
            <div className={styles.initiateMain}>
              <SectionCard n={1} title="Payment type">
                <div className={styles.railGrid}>
                  {PAYMENT_TYPES.map((t) => {
                    const active = form.rail === t.rail;
                    return (
                      <button
                        key={t.rail}
                        type="button"
                        className={[
                          styles.railTile,
                          active && styles.railTileActive,
                          !t.available && styles.railTileDisabled,
                        ].filter(Boolean).join(" ")}
                        onClick={() => { if (t.available) set("rail", t.rail); }}
                        disabled={!t.available}
                        aria-pressed={active}
                      >
                        <span className={styles.railTileGlyph}><Icon glyph={t.glyph} size={20} /></span>
                        <span className={styles.railTileText}>
                          <span className={styles.railTileLabel}>{t.label}</span>
                          <span className={styles.railTileBlurb}>{t.blurb}</span>
                        </span>
                        <Badge variant={t.available ? "green" : "gray"}>
                          {t.available ? `Phase ${t.phase}` : "Phase 2"}
                        </Badge>
                      </button>
                    );
                  })}
                </div>
              </SectionCard>

              <SectionCard n={2} title="Transfer">
                <div className={styles.transferPath}>
                  <TransferCard kind="From">
                    <div className={styles.fieldStack}>
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
                            {`${a.type} ····${String(a.accountNumber || "").slice(-4)} · ${a.currency || "USD"}`}
                          </Option>
                        ))}
                      </Select>

                      {debtor?.balance?.available != null && (
                        <button
                          type="button"
                          className={styles.balanceChip}
                          onClick={() => set("amount", String(debtor.balance.available))}
                          title="Use the whole available balance"
                        >
                          <Icon glyph="Wallet" size={16} />
                          Available {fmtAmount(debtor.balance.available, debtor.currency)} — use
                        </button>
                      )}
                    </div>
                  </TransferCard>

                  {/* Amount hero */}
                  <div className={styles.amountHero}>
                    <div className={styles.amountHeroTop}>
                      <label className={styles.amountHeroLabel}>Amount</label>
                      <Select
                        label="Currency"
                        value={form.currency}
                        onChange={(v) => set("currency", v)}
                        allowDeselect={false}
                        className={styles.currencySelect}
                      >
                        {["USD", "EUR", "GBP", "CAD", "CHF"].map((c) => (
                          <Option key={c} value={c}>{c}</Option>
                        ))}
                      </Select>
                    </div>
                    <div className={styles.amountRow}>
                      <input
                        className={[styles.amountInput, (err("amount") ? styles.amountInputError : "")].filter(Boolean).join(" ")}
                        value={form.amount}
                        onChange={(e) => set("amount", e.target.value)}
                        inputMode="decimal"
                        placeholder="0.00"
                        aria-label="Payment amount"
                      />
                      <span className={styles.amountCurrency}>{form.currency}</span>
                    </div>
                    {err("amount") && <div className={styles.amountError}>{err("amount")}</div>}
                    {amountOverBalance && (
                      <div className={styles.amountError}>
                        Exceeds the account's available balance.
                      </div>
                    )}
                    <div className={styles.quickChips}>
                      {QUICK_AMOUNTS.map((n) => (
                        <button
                          key={n}
                          type="button"
                          className={styles.chip}
                          onClick={() => set("amount", String(n))}
                        >
                          {fmtAmount(n, form.currency)}
                        </button>
                      ))}
                      {chipUseAvailable && (
                        <button
                          type="button"
                          className={styles.chip}
                          onClick={() => set("amount", String(debtor.balance.available))}
                        >
                          Max
                        </button>
                      )}
                    </div>
                  </div>

                  <TransferCard kind="To">
                    {isWire(form) ? (
                      <div className={styles.fieldStack}>
                        <div className={styles.flowHint}>
                          <Icon glyph="Bank" size={16} />
                          <span>External beneficiary — enter the payee's own details.</span>
                        </div>
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
                        <div className={styles.twoCol}>
                          <TextInput
                            label="Beneficiary Country"
                            description="2-letter ISO code"
                            value={form.beneficiaryCountry}
                            onChange={(e) => set("beneficiaryCountry", e.target.value.toUpperCase())}
                            errorMessage={err("beneficiaryCountry")}
                            state={state("beneficiaryCountry")}
                          />
                          <TextInput
                            label="SWIFT / BIC"
                            value={form.bic}
                            onChange={(e) => set("bic", e.target.value.toUpperCase())}
                            errorMessage={err("bic")}
                            state={state("bic")}
                          />
                        </div>
                        <TextInput
                          label="Beneficiary Address"
                          optional
                          value={form.beneficiaryAddress}
                          onChange={(e) => set("beneficiaryAddress", e.target.value)}
                        />
                      </div>
                    ) : (
                      <div className={styles.fieldStack}>
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
                                {`${a.ownerName ? a.ownerName + " · " : ""}${a.type} ····${String(a.accountNumber || "").slice(-4)}`}
                              </Option>
                            ))}
                        </Select>
                        <div className={styles.flowHint}>
                          <Icon glyph="Building" size={16} />
                          <span>
                            Transfer type (own / third party) is derived from whether the
                            beneficiary shares {customer?.identification?.legalName || "the payer"}'s customer.
                          </span>
                        </div>
                      </div>
                    )}
                  </TransferCard>
                </div>
              </SectionCard>

              <SectionCard n={3} title="Payment details">
                <div className={styles.twoCol}>
                  <div className={styles.fieldStack}>
                    <TextInput
                      label="Payment Purpose / Description"
                      value={form.purpose}
                      onChange={(e) => set("purpose", e.target.value)}
                      errorMessage={err("purpose")}
                      state={state("purpose")}
                    />
                    <Select
                      label="Category Purpose"
                      description="ISO ExternalPurpose code. Stage 3 resolves it against the purposeCodes table."
                      placeholder="None"
                      value={form.categoryPurpose}
                      onChange={(v) => set("categoryPurpose", v)}
                    >
                      {PURPOSE_CATEGORIES.map((g) => (
                        <OptionGroup key={g.label} label={g.label}>
                          {g.options.map(([code, name]) => (
                            <Option key={code} value={code}>{`${code} — ${name}`}</Option>
                          ))}
                        </OptionGroup>
                      ))}
                    </Select>
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
                    <div className={styles.colHeading}>Execution</div>
                    <label className={styles.priorityLabel}>Priority</label>
                    <SegmentedControl
                      value={form.priority}
                      onChange={(v) => set("priority", v)}
                      size="small"
                    >
                      {PRIORITIES.map((p) => (
                        <SegmentedControlOption key={p} value={p} label={p} />
                      ))}
                    </SegmentedControl>

                    <Select
                      label="Charges"
                      description="Applies to wire transfers."
                      value={form.chargeBearer}
                      onChange={(v) => set("chargeBearer", v)}
                      allowDeselect={false}
                    >
                      {CHARGE_BEARERS.map(([v, l]) => (
                        <Option key={v} value={v}>{l}</Option>
                      ))}
                    </Select>

                    <TextInput
                      label="Client Reference"
                      description="PO number, invoice ID — your own record."
                      optional
                      value={form.clientReference}
                      onChange={(e) => set("clientReference", e.target.value)}
                    />
                    <TextInput
                      label="End-to-End Reference"
                      description="Carried unchanged to the beneficiary."
                      optional
                      value={form.endToEndReference}
                      onChange={(e) => set("endToEndReference", e.target.value)}
                    />
                  </div>
                </div>

                {isWire(form) && (
                  <div className={styles.wireEnvelope}>
                    <div className={styles.subPanelTitle}>Wire routing &amp; processing</div>
                    <div className={styles.twoCol}>
                      <div className={styles.fieldStack}>
                        <TextInput
                          label="Beneficiary Bank Name"
                          value={form.beneficiaryBankName}
                          onChange={(e) => set("beneficiaryBankName", e.target.value)}
                          errorMessage={err("beneficiaryBankName")}
                          state={state("beneficiaryBankName")}
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
                      </div>
                      <div className={styles.fieldStack}>
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
                        <button type="button" className={styles.disclosure} onClick={() => setShowAdvanced((v) => !v)} aria-expanded={showAdvanced}>
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
                          </>
                        )}
                      </div>
                    </div>
                  </div>
                )}

                <div className={styles.infoBox}>
                  <div className={styles.infoBoxTitle}>Phase information</div>
                  <div><Badge variant="green">Phase 1</Badge> Wires, Internal Transfer</div>
                  <div style={{ marginTop: 4 }}><Badge variant="gray">Phase 2</Badge> ACH, Cards</div>
                </div>
              </SectionCard>
            </div>

            {/* ── live summary (right) ── */}
            <OrderSummary
              form={form}
              customer={customer}
              debtor={debtor}
              recipientLabel={recipientLabel}
              isOwnAccount={isOwnAccount}
              selectedType={selectedType}
            />
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
