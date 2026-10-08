"use client";

// "Generate Incoming Wire": the presenter's form for the simulated external pacs.008 (Doina
// Oct 6). It replaces the one-click happy-path-only trigger, so the 2026-09-29 "no picker"
// decision no longer holds: Wire Type, amount, currencies, sending bank and scenario are all
// chosen here. The ambient background simulator is unchanged and still happy-path only.
//
// Domestic vs international is not sent as a message field: the backend derives it from the
// originator BIC's country (characters 5-6) against ours (US). So the BIC list below is
// filtered by Wire Type, and a typed BIC whose country contradicts it is flagged here
// before the backend would refuse it.
import { useEffect, useState } from "react";
import Modal from "@leafygreen-ui/modal";
import Button from "@leafygreen-ui/button";
import Banner from "@leafygreen-ui/banner";
import TextInput from "@leafygreen-ui/text-input";
import { Select, Option } from "@leafygreen-ui/select";
import { H3 } from "@leafygreen-ui/typography";

import styles from "./PaymentsWorkflow.module.css";
import { useSimulateInboundWire } from "@/lib/api/hooks";

const HOME_COUNTRY = "US";
const BIC_PATTERN = /^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$/;

// Mirrors `simulate.SENDERS` — the BICs the backend resolves to a named bank. Any other
// well-formed BIC is accepted and gets a generated originator, so the field is free text
// with these as suggestions. Literal rather than fetched, like the rail list: it is a
// contract with the backend, not data.
const KNOWN_SENDERS = [
  { bic: "CHASUS33", label: "JPMorgan Chase (US)" },
  { bic: "DEUTDEFF", label: "Deutsche Bank (DE)" },
  { bic: "BARCGB22", label: "Barclays (GB)" },
  { bic: "UBSWCHZH", label: "UBS (CH)" },
  { bic: "NDEAFIHH", label: "Nordea (FI)" },
];

const ORIGINATOR_CURRENCIES = ["USD", "CAD", "EUR", "GBP"];
// CAD leads: the case Doina asked for is a CAD wire landing in a USD account.
const BENEFICIARY_CURRENCIES = ["CAD", "USD", "EUR", "GBP"];

const DEFAULT_BIC = { DOMESTIC: "CHASUS33", INTERNATIONAL: "DEUTDEFF" };

const SCENARIOS = [
  { value: "HAPPY", label: "Accept normally" },
  { value: "SANCTIONS", label: "Sanctions hit" },
];

const INITIAL = {
  wireType: "INTERNATIONAL",
  amount: "10,000.00",
  originatorCurrency: "CAD",
  beneficiaryCurrency: "USD",
  originatingBankBic: DEFAULT_BIC.INTERNATIONAL,
  scenario: "HAPPY",
};

function wireTypeOfBic(bic) {
  return bic.slice(4, 6) === HOME_COUNTRY ? "DOMESTIC" : "INTERNATIONAL";
}

function parseAmount(text) {
  const n = Number(String(text).replace(/,/g, "").trim());
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** The first problem with the form, as the sentence to show; null when it can be sent. */
function validate(form) {
  const bic = form.originatingBankBic.trim().toUpperCase();
  const amount = parseAmount(form.amount);
  return {
    amount: amount === null ? "Enter an amount greater than 0." : null,
    bic: !BIC_PATTERN.test(bic)
      ? "A BIC is 8 or 11 letters/digits, e.g. DEUTDEFF."
      : wireTypeOfBic(bic) !== form.wireType
        ? `${bic} is a ${wireTypeOfBic(bic).toLowerCase()} BIC. Change Wire Type or pick another.`
        : null,
  };
}

export default function GenerateIncomingWireModal({ open, onClose, onGenerated }) {
  const [form, setForm] = useState(INITIAL);
  const { simulate, busy, error, clearError } = useSimulateInboundWire();

  // Reopening starts clean: a stale refusal from the last attempt would read as current.
  useEffect(() => {
    if (open) {
      setForm(INITIAL);
      clearError();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const set = (patch) => setForm((f) => ({ ...f, ...patch }));

  const setWireType = (wireType) =>
    setForm((f) => ({
      ...f,
      wireType,
      // A domestic wire has one currency: the beneficiary's wins, as on the backend.
      originatorCurrency: wireType === "DOMESTIC" ? f.beneficiaryCurrency : f.originatorCurrency,
      // Keep a typed BIC that still fits; otherwise fall back to the type's default sender.
      originatingBankBic:
        BIC_PATTERN.test(f.originatingBankBic.trim().toUpperCase()) &&
        wireTypeOfBic(f.originatingBankBic.trim().toUpperCase()) === wireType
          ? f.originatingBankBic
          : DEFAULT_BIC[wireType],
    }));

  const domestic = form.wireType === "DOMESTIC";
  const problems = validate(form);
  const canSubmit = !busy && !problems.amount && !problems.bic;
  const fxExpected = !domestic && form.originatorCurrency !== form.beneficiaryCurrency;
  const suggestions = KNOWN_SENDERS.filter((s) => wireTypeOfBic(s.bic) === form.wireType);

  const generate = async () => {
    const paymentId = await simulate({
      scenario: form.scenario,
      wireType: form.wireType,
      amount: parseAmount(form.amount),
      originatorCurrency: form.originatorCurrency,
      beneficiaryCurrency: form.beneficiaryCurrency,
      originatingBankBic: form.originatingBankBic.trim().toUpperCase(),
    });
    if (paymentId) onGenerated(paymentId);
  };

  return (
    <Modal open={open} setOpen={(v) => !v && !busy && onClose()} contentClassName={styles.wireModal}>
      <H3>Generate Incoming Wire</H3>

      <div className={styles.wireForm}>
        <Select
          label="Wire Type"
          value={form.wireType}
          onChange={setWireType}
          allowDeselect={false}
        >
          <Option value="DOMESTIC">Domestic</Option>
          <Option value="INTERNATIONAL">International</Option>
        </Select>

        <TextInput
          label="Amount"
          value={form.amount}
          onChange={(e) => set({ amount: e.target.value })}
          inputMode="decimal"
          state={problems.amount ? "error" : "none"}
          errorMessage={problems.amount || undefined}
        />

        <div className={styles.wireRow}>
          <Select
            label="Originator Currency"
            value={form.originatorCurrency}
            onChange={(v) =>
              set(domestic ? { originatorCurrency: v, beneficiaryCurrency: v } : { originatorCurrency: v })
            }
            allowDeselect={false}
          >
            {ORIGINATOR_CURRENCIES.map((c) => (
              <Option key={c} value={c}>{c}</Option>
            ))}
          </Select>
          <Select
            label="Beneficiary Currency"
            value={form.beneficiaryCurrency}
            onChange={(v) =>
              set(domestic ? { originatorCurrency: v, beneficiaryCurrency: v } : { beneficiaryCurrency: v })
            }
            allowDeselect={false}
          >
            {BENEFICIARY_CURRENCIES.map((c) => (
              <Option key={c} value={c}>{c}</Option>
            ))}
          </Select>
        </div>
        {domestic && (
          <div className={styles.wireHint}>A domestic wire is sent and received in one currency.</div>
        )}
        {fxExpected && (
          <div className={styles.wireHint}>
            FX conversion expected: {form.originatorCurrency} to {form.beneficiaryCurrency} at
            the simulated rate.
          </div>
        )}

        {/* @leafygreen-ui/combobox is not installed; a native datalist on the LG TextInput
            gives the same "known senders plus free text" behaviour with no new dependency. */}
        <TextInput
          label="Originating Bank BIC"
          value={form.originatingBankBic}
          onChange={(e) => set({ originatingBankBic: e.target.value })}
          list="incoming-wire-known-bics"
          autoComplete="off"
          state={problems.bic ? "error" : "none"}
          errorMessage={problems.bic || undefined}
        />
        <datalist id="incoming-wire-known-bics">
          {suggestions.map((s) => (
            <option key={s.bic} value={s.bic}>{s.label}</option>
          ))}
        </datalist>

        <Select
          label="Scenario"
          value={form.scenario}
          onChange={(v) => set({ scenario: v })}
          allowDeselect={false}
        >
          {SCENARIOS.map((s) => (
            <Option key={s.value} value={s.value}>{s.label}</Option>
          ))}
        </Select>

        {error && <Banner variant="danger">{error}</Banner>}
      </div>

      <div className={styles.wireFooter}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button variant="primary" onClick={generate} disabled={!canSubmit}>
          {busy ? "Generating…" : "Generate Incoming Wire"}
        </Button>
      </div>
    </Modal>
  );
}
