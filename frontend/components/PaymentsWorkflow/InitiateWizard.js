"use client";

// Bank-assisted payment initiation (doc 16 §7b).
//
// Doina's mockup is customer self-service — Jane Smith moving her own money. This is the
// bank-assisted variant: an employee creating the instruction on a customer's behalf,
// which is the Canadian retail model in her Frontend research. Her research is explicit
// that the two models differ ONLY at the initiation layer (actor, channel, controls) and
// share everything downstream, so the wizard mirrors her four steps with one addition: a
// customer-selection step in front, because the employee must say who they are acting for.
//
// Concretely that means `channel: "BRANCH"` rather than WEB/MOBILE, and the guidance rail
// from her mockup carries the entitlement result instead of reassurance copy.
import { useMemo, useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Banner from "@leafygreen-ui/banner";
import Button from "@leafygreen-ui/button";
import Card from "@leafygreen-ui/card";
import Icon from "@leafygreen-ui/icon";
import TextInput from "@leafygreen-ui/text-input";
import { Select, Option } from "@leafygreen-ui/select";
import { SegmentedControl, SegmentedControlOption } from "@leafygreen-ui/segmented-control";
import Stepper, { Step } from "@leafygreen-ui/stepper";
import { Body, H3 } from "@leafygreen-ui/typography";

import styles from "./PaymentsWorkflow.module.css";
import { coreApi } from "@/lib/api/client";
import { useBankAssistedParties } from "@/lib/api/hooks";
import { fmtAmount } from "@/lib/paymentsWorkflow/status";

const STEPS = ["Customer", "Transfer details", "Recipient", "Review & authorize"];

// Destination shapes the rail, which in turn decides whether the recipient is one of our
// own accounts or an external party. Two choices, mirroring her "Within Canada /
// International" control, but expressed in the terms this demo actually models.
const DESTINATION = { INTERNAL: "INTERNAL", WIRE: "WIRE" };

const CLEARING_SYSTEMS = ["USABA", "USPID", "GBDSC", "CHBCC", "DEBLZ", "CACPA"];
const ACCOUNT_TYPES = ["Checking", "Savings", "Current", "FixedDeposit"];

const EMPTY = {
  customerId: "",
  debtorAccountId: "",
  destination: DESTINATION.INTERNAL,
  amount: "",
  currency: "USD",
  requestedExecutionDate: "",
  priority: "NORMAL",
  remittance: "",
  // internal recipient
  creditorAccountId: "",
  // external recipient
  creditorName: "",
  creditorAccountNo: "",
  creditorBic: "",
  creditorBankName: "",
  creditorBankCountry: "",
  creditorAddress: "",
  creditorAccountType: "Checking",
  creditorClearingSystemCode: "",
  creditorClearingSystemMemberId: "",
};

/** Build the PaymentOrderInitiation/Initiate body from wizard state. */
function buildPayload(form) {
  const isWire = form.destination === DESTINATION.WIRE;
  const payload = {
    customerId: form.customerId,
    type: "CREDIT_TRANSFER",
    rail: isWire ? "WIRE" : "INTERNAL",
    debtor: { accountId: form.debtorAccountId },
    instructedAmount: Number(form.amount),
    instructedCurrency: form.currency.toUpperCase(),
    priority: form.priority,
    // The defining difference between this surface and the customer portal.
    channel: "BRANCH",
  };

  if (form.requestedExecutionDate) {
    payload.requestedExecutionDate = form.requestedExecutionDate;
  }
  if (form.remittance) {
    payload.remittance = { unstructured: form.remittance };
  }

  if (isWire) {
    // An external creditor carries its own identity; the contract requires name +
    // accountNo, and a BIC once there is no accountId (api_models cross-field rules).
    payload.creditor = {
      name: form.creditorName,
      accountNo: form.creditorAccountNo,
      bic: form.creditorBic,
      bankName: form.creditorBankName || null,
      bankCountry: form.creditorBankCountry ? form.creditorBankCountry.toUpperCase() : null,
      address: form.creditorAddress || null,
      accountType: form.creditorAccountType || null,
      clearingSystemCode: form.creditorClearingSystemCode || null,
      clearingSystemMemberId: form.creditorClearingSystemMemberId || null,
    };
    // Drop nulls: the request model forbids unknown fields but is happy without optionals,
    // and sending explicit nulls muddies the stored envelope.
    Object.keys(payload.creditor).forEach((k) => {
      if (payload.creditor[k] === null || payload.creditor[k] === "") delete payload.creditor[k];
    });
  } else {
    // An account we hold — the snapshot (name, BIC, address) resolves server-side.
    payload.creditor = { accountId: form.creditorAccountId };
  }

  return payload;
}

/** Per-step completeness. Gates the forward button so the API is not asked to say no. */
function stepErrors(step, form) {
  const errors = {};
  if (step === 0) {
    if (!form.customerId) errors.customerId = "Select a customer.";
    if (!form.debtorAccountId) errors.debtorAccountId = "Select the account to debit.";
  }
  if (step === 1) {
    const amount = Number(form.amount);
    if (!form.amount) errors.amount = "Enter an amount.";
    else if (Number.isNaN(amount) || amount <= 0) errors.amount = "Amount must be greater than zero.";
    if (!/^[A-Za-z]{3}$/.test(form.currency)) errors.currency = "Use a 3-letter ISO code.";
  }
  if (step === 2) {
    if (form.destination === DESTINATION.INTERNAL) {
      if (!form.creditorAccountId) errors.creditorAccountId = "Select a recipient account.";
      else if (form.creditorAccountId === form.debtorAccountId) {
        errors.creditorAccountId = "Recipient must differ from the debit account.";
      }
    } else {
      if (!form.creditorName) errors.creditorName = "Legal name is required.";
      if (!form.creditorAccountNo) errors.creditorAccountNo = "Account number is required.";
      // The spec's validator requires a creditor BIC on the WIRE rail whenever the
      // creditor is external (api_models cross-field rule).
      if (!form.creditorBic) errors.creditorBic = "BIC is required for an external wire.";
    }
  }
  return errors;
}

function GuidanceRail({ step, form, customer, debtorAccount, recipientLabel }) {
  const isWire = form.destination === DESTINATION.WIRE;
  return (
    <div className={styles.railStack}>
      <Card>
        <div className={styles.panelTitle} style={{ marginBottom: 12 }}>Transfer summary</div>
        <table className={styles.kv}>
          <tbody>
            <tr>
              <td>Customer</td>
              <td>{customer?.identification?.legalName || form.customerId || "—"}</td>
            </tr>
            <tr>
              <td>From</td>
              <td>
                {debtorAccount
                  ? `${debtorAccount.type} ···· ${String(debtorAccount.accountNumber || "").slice(-4)}`
                  : "—"}
              </td>
            </tr>
            <tr>
              <td>Available</td>
              <td>
                {debtorAccount?.balance?.available != null
                  ? fmtAmount(debtorAccount.balance.available, debtorAccount.currency)
                  : "—"}
              </td>
            </tr>
            <tr>
              <td>Recipient</td>
              <td>{recipientLabel || "—"}</td>
            </tr>
            <tr>
              <td>Amount</td>
              <td>{form.amount ? fmtAmount(form.amount, form.currency) : "—"}</td>
            </tr>
            <tr>
              <td>Rail</td>
              <td>{isWire ? "WIRE" : "INTERNAL"}</td>
            </tr>
            <tr>
              <td>Channel</td>
              <td>BRANCH (bank-assisted)</td>
            </tr>
          </tbody>
        </table>
      </Card>

      {step === 2 && isWire && (
        <Banner variant="warning">
          Check the recipient&apos;s details carefully. Wire transfers may be difficult or
          impossible to recover after they are sent.
        </Banner>
      )}

      {step === 3 && (
        <Card>
          <div className={styles.panelTitle} style={{ marginBottom: 12 }}>
            Entitlement checks
          </div>
          {/* Stage 2 populates payments.checks[]; until then this is honest about being
              empty rather than showing five ticks the backend never asserted. */}
          <Body className={styles.muted}>
            Authentication, signatory authority, account status, restrictions, limit and
            dual-approval checks are recorded on the payment from stage 2. This payment will
            show them in its lifecycle trace once that stage lands.
          </Body>
          <div style={{ marginTop: 12 }}>
            <Badge variant="lightgray">Pending stage 2</Badge>
          </div>
        </Card>
      )}

      {isWire && step < 2 && (
        <Banner variant="info">
          External wires are captured and validated, then held at SUBMITTED — rail execution
          arrives in stage 5.
        </Banner>
      )}
    </div>
  );
}

export default function InitiateWizard({ onInitiated }) {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState(EMPTY);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  const [showErrors, setShowErrors] = useState(false);

  const { customers, accountsByCustomer, allAccounts, loading } = useBankAssistedParties();

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const customer = customers.find((c) => c.customerId === form.customerId) || null;
  const customerAccounts = accountsByCustomer.get(form.customerId) || [];
  const debtorAccount = customerAccounts.find((a) => a.accountId === form.debtorAccountId) || null;

  const errors = useMemo(() => stepErrors(step, form), [step, form]);
  const stepValid = Object.keys(errors).length === 0;
  const err = (k) => (showErrors ? errors[k] : undefined);

  const recipientLabel =
    form.destination === DESTINATION.WIRE
      ? form.creditorName || null
      : allAccounts.find((a) => a.accountId === form.creditorAccountId)?.accountNumber || null;

  const next = () => {
    if (!stepValid) {
      setShowErrors(true);
      return;
    }
    setShowErrors(false);
    setStep((s) => Math.min(s + 1, STEPS.length - 1));
  };

  const back = () => {
    setShowErrors(false);
    setStep((s) => Math.max(s - 1, 0));
  };

  const reset = () => {
    setForm(EMPTY);
    setStep(0);
    setShowErrors(false);
    setSubmitError(null);
  };

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
    const paymentId = data?.paymentId || data?.payment_id || data?.id || null;
    reset();
    onInitiated?.(paymentId);
  }

  return (
    <div className={styles.wizard}>
      <div className={styles.panel}>
        <div className={styles.panelBody}>
          <H3>Initiate a transfer</H3>
          <Body className={styles.subtitle}>
            Create a wire or internal transfer on behalf of a Leafy Bank customer.
          </Body>

          <div className={styles.stepperWrap} style={{ marginTop: 16 }}>
            <Stepper currentStep={step} maxDisplayedSteps={4}>
              {STEPS.map((label) => (
                <Step key={label}>{label}</Step>
              ))}
            </Stepper>
          </div>

          {loading && <div className={styles.emptyState}>Loading customers and accounts…</div>}

          {!loading && step === 0 && (
            <div className={styles.formGrid}>
              <div className={styles.formGridFull}>
                <Select
                  label="Customer"
                  description="The customer this transfer is being created for."
                  placeholder="Select a customer"
                  value={form.customerId}
                  onChange={(v) => setForm((f) => ({ ...f, customerId: v, debtorAccountId: "" }))}
                  errorMessage={err("customerId")}
                  state={err("customerId") ? "error" : "none"}
                  allowDeselect={false}
                >
                  {customers.map((c) => (
                    <Option key={c.customerId} value={c.customerId}>
                      {c.identification?.legalName || c.customerId} ({c.customerId})
                    </Option>
                  ))}
                </Select>
              </div>
              <div className={styles.formGridFull}>
                <Select
                  label="From account"
                  description={
                    debtorAccount?.balance?.available != null
                      ? `Available balance: ${fmtAmount(debtorAccount.balance.available, debtorAccount.currency)}`
                      : "The account to debit."
                  }
                  placeholder={form.customerId ? "Select an account" : "Select a customer first"}
                  value={form.debtorAccountId}
                  onChange={(v) => set("debtorAccountId", v)}
                  disabled={!form.customerId}
                  errorMessage={err("debtorAccountId")}
                  state={err("debtorAccountId") ? "error" : "none"}
                  allowDeselect={false}
                >
                  {customerAccounts.map((a) => (
                    <Option key={a.accountId} value={a.accountId}>
                      {a.type} ···· {String(a.accountNumber || "").slice(-4)} — {fmtAmount(a.balance?.available, a.currency)}
                    </Option>
                  ))}
                </Select>
              </div>
            </div>
          )}

          {!loading && step === 1 && (
            <div className={styles.formGrid}>
              <div className={styles.formGridFull}>
                <span className={styles.filterLabel}>Where is the money going?</span>
                <div style={{ marginTop: 4 }}>
                  <SegmentedControl
                    name="pw-destination"
                    value={form.destination}
                    onChange={(v) => set("destination", v)}
                    aria-label="Transfer destination"
                    aria-controls="pw-recipient-step"
                  >
                    <SegmentedControlOption value={DESTINATION.INTERNAL}>
                      Within Leafy Bank
                    </SegmentedControlOption>
                    <SegmentedControlOption value={DESTINATION.WIRE}>
                      External wire
                    </SegmentedControlOption>
                  </SegmentedControl>
                </div>
              </div>
              <TextInput
                label="Send amount"
                type="number"
                value={form.amount}
                onChange={(e) => set("amount", e.target.value)}
                errorMessage={err("amount")}
                state={err("amount") ? "error" : "none"}
              />
              <TextInput
                label="Currency"
                description="ISO 4217"
                value={form.currency}
                onChange={(e) => set("currency", e.target.value.toUpperCase())}
                errorMessage={err("currency")}
                state={err("currency") ? "error" : "none"}
              />
              <TextInput
                label="Requested execution date"
                type="date"
                optional
                value={form.requestedExecutionDate}
                onChange={(e) => set("requestedExecutionDate", e.target.value)}
              />
              <Select
                label="Priority"
                value={form.priority}
                onChange={(v) => set("priority", v)}
                allowDeselect={false}
              >
                {["NORMAL", "HIGH", "URGENT"].map((p) => (
                  <Option key={p} value={p}>{p}</Option>
                ))}
              </Select>
              <div className={styles.formGridFull}>
                <TextInput
                  label="Remittance information"
                  optional
                  value={form.remittance}
                  onChange={(e) => set("remittance", e.target.value)}
                />
              </div>
            </div>
          )}

          {!loading && step === 2 && form.destination === DESTINATION.INTERNAL && (
            <div className={styles.formGrid} id="pw-recipient-step">
              <div className={styles.formGridFull}>
                <Select
                  label="Recipient account"
                  description="Any Leafy Bank current or savings account."
                  placeholder="Select a recipient"
                  value={form.creditorAccountId}
                  onChange={(v) => set("creditorAccountId", v)}
                  errorMessage={err("creditorAccountId")}
                  state={err("creditorAccountId") ? "error" : "none"}
                  allowDeselect={false}
                >
                  {allAccounts
                    .filter((a) => a.accountId !== form.debtorAccountId)
                    .map((a) => (
                      <Option key={a.accountId} value={a.accountId}>
                        {a.ownerName ? `${a.ownerName} — ` : ""}{a.type} ···· {String(a.accountNumber || "").slice(-4)}
                      </Option>
                    ))}
                </Select>
              </div>
            </div>
          )}

          {!loading && step === 2 && form.destination === DESTINATION.WIRE && (
            <div className={styles.formGrid} id="pw-recipient-step">
              <div className={styles.formGridFull}>
                <TextInput
                  label="Legal name"
                  value={form.creditorName}
                  onChange={(e) => set("creditorName", e.target.value)}
                  errorMessage={err("creditorName")}
                  state={err("creditorName") ? "error" : "none"}
                />
              </div>
              <TextInput
                label="Account number"
                value={form.creditorAccountNo}
                onChange={(e) => set("creditorAccountNo", e.target.value)}
                errorMessage={err("creditorAccountNo")}
                state={err("creditorAccountNo") ? "error" : "none"}
              />
              <TextInput
                label="BIC / SWIFT"
                value={form.creditorBic}
                onChange={(e) => set("creditorBic", e.target.value.toUpperCase())}
                errorMessage={err("creditorBic")}
                state={err("creditorBic") ? "error" : "none"}
              />
              <TextInput
                label="Bank name"
                optional
                value={form.creditorBankName}
                onChange={(e) => set("creditorBankName", e.target.value)}
              />
              <TextInput
                label="Bank country"
                description="2-letter ISO code"
                optional
                value={form.creditorBankCountry}
                onChange={(e) => set("creditorBankCountry", e.target.value.toUpperCase())}
              />
              <Select
                label="Account type"
                value={form.creditorAccountType}
                onChange={(v) => set("creditorAccountType", v)}
                allowDeselect={false}
              >
                {ACCOUNT_TYPES.map((t) => (
                  <Option key={t} value={t}>{t}</Option>
                ))}
              </Select>
              <Select
                label="Clearing system"
                placeholder="None"
                value={form.creditorClearingSystemCode}
                onChange={(v) => set("creditorClearingSystemCode", v)}
              >
                {CLEARING_SYSTEMS.map((c) => (
                  <Option key={c} value={c}>{c}</Option>
                ))}
              </Select>
              <TextInput
                label="Clearing member ID"
                optional
                value={form.creditorClearingSystemMemberId}
                onChange={(e) => set("creditorClearingSystemMemberId", e.target.value)}
              />
              <div className={styles.formGridFull}>
                <TextInput
                  label="Address"
                  optional
                  value={form.creditorAddress}
                  onChange={(e) => set("creditorAddress", e.target.value)}
                />
              </div>
            </div>
          )}

          {!loading && step === 3 && (
            <>
              <table className={styles.kv} style={{ marginTop: 8 }}>
                <tbody>
                  <tr><td>Customer</td><td>{customer?.identification?.legalName || form.customerId}</td></tr>
                  <tr><td>From account</td><td>{debtorAccount?.accountNumber || form.debtorAccountId}</td></tr>
                  <tr><td>Recipient</td><td>{recipientLabel || "—"}</td></tr>
                  <tr><td>Amount</td><td>{fmtAmount(form.amount, form.currency)}</td></tr>
                  <tr><td>Rail</td><td>{form.destination === DESTINATION.WIRE ? "WIRE" : "INTERNAL"}</td></tr>
                  <tr><td>Priority</td><td>{form.priority}</td></tr>
                  <tr><td>Execution date</td><td>{form.requestedExecutionDate || "Today"}</td></tr>
                  <tr><td>Remittance</td><td>{form.remittance || "—"}</td></tr>
                </tbody>
              </table>
              {submitError && (
                <div style={{ marginTop: 16 }}>
                  <Banner variant="danger">Could not initiate — {submitError}</Banner>
                </div>
              )}
            </>
          )}

          <div className={styles.formActions}>
            <Button onClick={back} disabled={step === 0 || submitting}>Back</Button>
            <div className={styles.formActionsRight}>
              <Button variant="default" onClick={reset} disabled={submitting}>Cancel</Button>
              {step < STEPS.length - 1 ? (
                <Button
                  variant="primary"
                  onClick={next}
                  disabled={loading}
                  rightGlyph={<Icon glyph="ArrowRight" />}
                >
                  Continue
                </Button>
              ) : (
                <Button variant="primary" onClick={submit} disabled={submitting}>
                  {submitting ? "Submitting…" : "Submit transfer"}
                </Button>
              )}
            </div>
          </div>
        </div>
      </div>

      <GuidanceRail
        step={step}
        form={form}
        customer={customer}
        debtorAccount={debtorAccount}
        recipientLabel={recipientLabel}
      />
    </div>
  );
}
