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

import styles from "./PaymentsWorkflow.module.css";
import StepUpModal from "@/components/StepUpModal/StepUpModal";
import { coreApi } from "@/lib/api/client";
import { isStepUpRequired } from "@/lib/api/partyAuthentication";
import { useBankAssistedParties } from "@/lib/api/hooks";
import { fmtAmount } from "@/lib/paymentsWorkflow/status";

// Stepper stops at Confirmation. An "Approve" step is deliberately absent: dual approval
// is stage 2 (doc 15 B4) and has neither a threshold nor an approver today, so showing it
// would promise a gate that does not run.
const STEPS = ["Enter details", "Review", "Confirmation"];

// Phase 1 is wires + internal transfers; ACH and cards are Phase 2. The unavailable rails
// are listed rather than hidden so the demo can point at the roadmap — `disabled` keeps
// them unselectable.
const PAYMENT_TYPES = [
  { rail: "WIRE", label: "Wires", phase: 1, available: true, glyph: "Building",
    blurb: "Real-time or near-real-time transfer of funds between banks." },
  { rail: "INTERNAL", label: "Internal Transfer", phase: 1, available: true, glyph: "Refresh",
    blurb: "Book transfer between two Leafy Bank accounts. Settles on our own ledger." },
  { rail: "ACH", label: "ACH", phase: 2, available: false, glyph: "Menu",
    blurb: "Batch clearing house transfers. Phase 2." },
  { rail: "CARD", label: "Cards", phase: 2, available: false, glyph: "CreditCard",
    blurb: "Card acquiring and issuing rails. Phase 2." },
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

    const wire = {};
    if (form.serviceLevelCode) wire.serviceLevel = { code: form.serviceLevelCode };
    if (form.localInstrumentCode) wire.localInstrument = { code: form.localInstrumentCode };
    // `wireType` is deliberately NOT sent — the server derives it from the two bank
    // countries and nulls it when either is unknown (doc 13, initiation_envelope.py).
    if (Object.keys(wire).length) payload.wireDetails = wire;
  } else {
    // An account we hold — name, BIC and address resolve from the snapshot server-side.
    payload.creditor = { accountId: form.creditorAccountId };
    payload.internalDetails = { transferType: form.transferType };
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

const EXTERNAL_BANKS = [
  { bankName: "JPMorgan Chase Bank, N.A.", bic: "CHASUS33", country: "US", clearingSystemCode: "USABA" },
  { bankName: "Citibank, N.A.", bic: "CITIUS33", country: "US", clearingSystemCode: "USABA" },
  { bankName: "Barclays Bank PLC", bic: "BARCGB22", country: "GB", clearingSystemCode: "GBDSC" },
  { bankName: "Deutsche Bank AG", bic: "DEUTDEFF", country: "DE", clearingSystemCode: "DEBLZ" },
  { bankName: "UBS Switzerland AG", bic: "UBSWCHZH", country: "CH", clearingSystemCode: "CHBCC" },
  { bankName: "Royal Bank of Canada", bic: "ROYCCAT2", country: "CA", clearingSystemCode: "CACPA" },
];

const EXTERNAL_PAYEES = [
  { name: "Northwind Traders Ltd", address: "12 Cheapside, London" },
  { name: "Contoso Manufacturing Inc", address: "440 Industrial Way, Detroit" },
  { name: "Fabrikam Logistics GmbH", address: "Hafenstrasse 8, Hamburg" },
  { name: "Tailspin Aviation SA", address: "Route de Meyrin 21, Geneva" },
  { name: "Adventure Works Supply Co", address: "77 Harbour Street, Toronto" },
];

const AUTOFILL_PURPOSES = [
  "Invoice settlement",
  "Supplier payment",
  "Consulting services",
  "Quarterly rent",
  "Freight and handling",
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

  const next = {
    ...EMPTY,
    rail: form.rail,
    customerId: debtor.customerId,
    debtorAccountId: debtor.account.accountId,
    currency: debtor.account.currency || "USD",
    amount,
    purpose: pick(AUTOFILL_PURPOSES),
    endToEndReference: `E2E-${digits(8)}`,
    clientReference: `INV-${digits(6)}`,
    chargeBearer: pick(CHARGE_BEARERS)[0],
    priority: pick(PRIORITIES),
  };

  if (isWire(next)) {
    const bank = pick(EXTERNAL_BANKS);
    const payee = pick(EXTERNAL_PAYEES);
    return {
      ...next,
      beneficiaryName: payee.name,
      beneficiaryAddress: payee.address,
      beneficiaryAccountNo: digits(10),
      beneficiaryCountry: bank.country,
      beneficiaryBankName: bank.bankName,
      bic: bank.bic,
      clearingSystemCode: bank.clearingSystemCode,
      clearingSystemMemberId: digits(9),
      accountNumberType: pick(ACCOUNT_TYPES),
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
    transferType: pick(TRANSFER_TYPES)[0],
  };
}

function SectionCard({ n, title, subtitle, children, className }) {
  return (
    <div className={`${styles.sectionCard} ${className || ""}`}>
      <div className={styles.sectionHeading}>
        <span>{n}.</span>
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
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  // Non-null while the step-up challenge is on screen; holds the backend's refusal text so
  // the modal can quote the reason it opened rather than paraphrasing it.
  const [stepUpReason, setStepUpReason] = useState(null);
  const [createdId, setCreatedId] = useState(null);

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
      // Stage 2 refused the factor, not the payment: the amount is over the segment's
      // step-up threshold. Collect a second factor and retry rather than surfacing a dead
      // end — the refusal is the trigger for the step-up, which is the point of it.
      if (isStepUpRequired(error)) {
        setStepUpReason(error);
        return;
      }
      setSubmitError(error);
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
            <Icon glyph="CheckmarkWithCircle" size={48} fill="#00684a" />
            <H2>Payment submitted</H2>
            <Body className={styles.muted}>
              The payment order was accepted and is moving through the lifecycle.
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
                View lifecycle
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
      ["Payment type", `${selectedType.label} (Phase ${selectedType.phase})`],
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
      ["Channel", "BRANCH (bank-assisted)"],
    ];
    return (
      <div className={styles.panel}>
        {/* Renders in a portal, so its position in the tree does not matter. It lives on
            the review step because that is the only step that submits. */}
        <StepUpModal
          open={stepUpReason !== null}
          reason={stepUpReason}
          onCancel={() => setStepUpReason(null)}
          onSuccess={() => {
            // The session is now two-factor. Retry the same payload: the amount and the
            // entitlement are unchanged, only the strength of the assertion moved.
            setStepUpReason(null);
            submit();
          }}
        />
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

        <div className={styles.stepperBar}>
          <Stepper currentStep={0} maxDisplayedSteps={3}>
            {STEPS.map((l) => <Step key={l}>{l}</Step>)}
          </Stepper>
        </div>

        {loading && <div className={styles.emptyState}>Loading customers and accounts…</div>}

        {!loading && (
          <div className={styles.createGrid}>
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
                      {`${t.label} (Phase ${t.phase})`}
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

                <div className={styles.infoBox}>
                  <div className={styles.infoBoxTitle}>Phase information</div>
                  <div>
                    <Badge variant="green">Phase 1</Badge> Wires, Internal Transfer
                  </div>
                  <div style={{ marginTop: 4 }}>
                    <Badge variant="lightgray">Phase 2</Badge> ACH, Cards
                  </div>
                </div>

                {isWire(form) && (
                  <Banner variant="info">
                    External wires are captured and validated, then held at SUBMITTED — rail
                    execution arrives in stage 5.
                  </Banner>
                )}
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
