"use client";

import React, { useState, useEffect } from "react";
import { H2, H3, Body } from "@leafygreen-ui/typography";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import styles from "./SendMoneyModal.module.css";
import { useUser } from "@/lib/context/UserContext";
import { useAccounts, useBeneficiaryAccounts } from "@/lib/api/hooks";
import { coreApi } from "@/lib/api/client";

// Drives the payment's rail, which in turn drives ledger postingMode routing
// (BATCH / NEAR_REALTIME / REALTIME — see backend/ledger/workers/ingest_worker.py).
const PAYMENT_METHODS = [
  { id: "debit", label: "Debit Card" },
  { id: "bank_transfer", label: "Bank Transfer" },
  { id: "wire", label: "Wire Transfer" },
  { id: "venmo", label: "Venmo" },
  { id: "paypal", label: "PayPal" },
];
const RAIL_BY_PAYMENT_METHOD = {
  debit: "INTERNAL",
  bank_transfer: "ACH",
  wire: "WIRE",
  venmo: "VENMO",
  paypal: "PAYPAL",
};

const TRANSFER_METHODS = [
  { id: "internal", label: "Internal Transfer" },
  { id: "ach", label: "ACH Transfer" },
  { id: "wire", label: "Wire Transfer" },
];
const RAIL_BY_TRANSFER_METHOD = {
  internal: "INTERNAL",
  ach: "ACH",
  wire: "WIRE",
};

// view: null = picker, "digital-payment" = digital payment form, "transfer" = transfer form
export default function SendMoneyModal({
  isOpen,
  onClose,
  initialView = null,
}) {
  const { selectedUser, refreshData } = useUser();
  const { accounts } = useAccounts();
  const { beneficiaryAccounts } = useBeneficiaryAccounts();
  const [view, setView] = useState(initialView);

  const [paymentAmount, setPaymentAmount] = useState("");
  const [paymentMethod, setPaymentMethod] = useState("");
  const [paymentOriginator, setPaymentOriginator] = useState("");
  const [paymentBeneficiary, setPaymentBeneficiary] = useState("");

  const [transferAmount, setTransferAmount] = useState("");
  const [transferMethod, setTransferMethod] = useState("internal");
  const [transferOriginator, setTransferOriginator] = useState("");
  const [transferBeneficiary, setTransferBeneficiary] = useState("");

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (isOpen) {
      setView(initialView ?? null);
    } else {
      setPaymentAmount(""); setPaymentMethod(""); setPaymentOriginator(""); setPaymentBeneficiary("");
      setTransferAmount(""); setTransferMethod("internal"); setTransferOriginator(""); setTransferBeneficiary("");
      setSubmitting(false); setError(null);
    }
  }, [isOpen, initialView]);

  if (!isOpen) return null;

  // Originator is always one of the logged-in customer's own accounts.
  // Beneficiary can be ANY bank account — prefetched bank-wide with owner
  // names attached, see useBeneficiaryAccounts.
  const customerId = selectedUser?.id ? `CUST-${selectedUser.id.slice(-8)}` : null;
  const accountOptions = accounts.map((a) => ({
    id: a.accountId,
    label: `${a.AccountType} - ${a.AccountNumber}`,
    currency: a.currency,
  }));

  const handleClose = () => {
    onClose();
  };

  const handleSubmit = async () => {
    const isTransfer = view === "transfer";
    const amount = Number(isTransfer ? transferAmount : paymentAmount);
    const originator = isTransfer ? transferOriginator : paymentOriginator;
    const beneficiary = isTransfer ? transferBeneficiary : paymentBeneficiary;
    const rail = (isTransfer ? RAIL_BY_TRANSFER_METHOD[transferMethod] : RAIL_BY_PAYMENT_METHOD[paymentMethod]) || "INTERNAL";

    if (!customerId) { setError("No customer selected."); return; }
    if (!amount || amount <= 0) { setError("Enter a valid amount."); return; }
    if (!originator || !beneficiary) { setError("Select an originator and beneficiary account."); return; }
    if (originator === beneficiary) {
      setError("Originator and beneficiary must differ.");
      return;
    }

    const currency =
      accountOptions.find((a) => a.id === originator)?.currency || "USD";

    setSubmitting(true);
    setError(null);
    const { error: err } = await coreApi("PaymentOrderInitiation/Initiate", {
      method: "POST",
      body: {
        customerId,
        // type is validated but cosmetic; the rail is what matters for posting mode.
        type: isTransfer ? "INTRABANK_TRANSFER" : "CREDIT_TRANSFER",
        rail,
        debtor: { accountId: originator },
        creditor: { accountId: beneficiary },
        instructedAmount: amount,
        instructedCurrency: currency,
      },
    });
    setSubmitting(false);

    if (err) {
      // coreApi returns "<status>: <body>"; surface the backend detail.
      setError(err.replace(/^\d+:\s*/, ""));
      return;
    }
    refreshData();
    onClose();
  };

  const renderBeneficiaryField = (idPrefix, beneficiary, setBeneficiary, originator) => (
    <div className={styles.formGroup}>
      <label className={styles.formLabel} htmlFor={`${idPrefix}-beneficiary`}>Beneficiary account</label>
      <select
        id={`${idPrefix}-beneficiary`}
        className={styles.formSelect}
        value={beneficiary}
        onChange={(e) => setBeneficiary(e.target.value)}
      >
        <option value="">Select beneficiary account</option>
        {beneficiaryAccounts
          .filter((a) => a.id !== originator)
          .map((a) => (
            <option key={a.id} value={a.id}>
              {a.ownerName ? `${a.ownerName} — ${a.label}` : a.label}
            </option>
          ))}
      </select>
    </div>
  );

  const title = view === "digital-payment"
    ? "Digital Payment"
    : view === "transfer"
    ? "Transfer Money"
    : "Send Money";

  return (
    <div className={styles.modalBackdrop} onClick={handleClose}>
      <div
        className={styles.modalDialog}
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <div className={styles.modalHeader}>
          <div className={styles.modalHeaderLeft}>
            {view !== null && initialView === null && (
              <button
                className={styles.backButton}
                onClick={() => setView(null)}
                aria-label="Back to options"
              >
                <Icon glyph="ArrowLeft" size="small" />
              </button>
            )}
            <H2>{title}</H2>
          </div>
          <button
            className={styles.modalCloseButton}
            onClick={handleClose}
            aria-label="Close modal"
          >
            <Icon glyph="X" />
          </button>
        </div>

        {view === null && (
          <div className={styles.pickerGrid}>
            <button className={styles.typeCard} onClick={() => setView("digital-payment")}>
              <div className={styles.typeIcon}>
                <Icon glyph="CreditCard" size="xlarge" />
              </div>
              <span className={styles.typeLabel}>Digital Payment</span>
              <span className={styles.typeDesc}>Pay a bill or merchant via card or wire</span>
            </button>
            <button className={styles.typeCard} onClick={() => setView("transfer")}>
              <div className={styles.typeIcon}>
                <Icon glyph="ArrowRight" size="xlarge" />
              </div>
              <span className={styles.typeLabel}>Transfer</span>
              <span className={styles.typeDesc}>Move money between accounts</span>
            </button>
          </div>
        )}

        {view === "digital-payment" && (
          <>
            <Body className={styles.modalSubtext}>Transaction limit 500</Body>
            <div className={styles.modalBody}>
              <div className={styles.formGroup}>
                <label className={styles.formLabel} htmlFor="payment-amount">Transaction amount</label>
                <input
                  id="payment-amount"
                  className={styles.formInput}
                  type="number"
                  value={paymentAmount}
                  onChange={(e) => setPaymentAmount(e.target.value)}
                  placeholder="Enter amount"
                />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.formLabel} htmlFor="payment-method">Payment method</label>
                <select
                  id="payment-method"
                  className={styles.formSelect}
                  value={paymentMethod}
                  onChange={(e) => setPaymentMethod(e.target.value)}
                >
                  <option value="">Select a payment method</option>
                  {PAYMENT_METHODS.map((pm) => (
                    <option key={pm.id} value={pm.id}>{pm.label}</option>
                  ))}
                </select>
              </div>
              <div className={styles.formGroup}>
                <label className={styles.formLabel} htmlFor="payment-originator">Originator account</label>
                <select
                  id="payment-originator"
                  className={styles.formSelect}
                  value={paymentOriginator}
                  onChange={(e) => setPaymentOriginator(e.target.value)}
                >
                  <option value="">Select originator account</option>
                  {accountOptions.map((a) => (
                    <option key={a.id} value={a.id}>{a.label}</option>
                  ))}
                </select>
              </div>
              {renderBeneficiaryField("payment", paymentBeneficiary, setPaymentBeneficiary, paymentOriginator)}
            </div>
            <div className={styles.modalActions}>
              {error && <Body style={{ color: "#DB3030" }}>{error}</Body>}
              <Button variant="default" onClick={handleClose} disabled={submitting}>Cancel</Button>
              <Button variant="primary" onClick={handleSubmit} disabled={submitting}>
                {submitting ? "Sending…" : "Submit"}
              </Button>
            </div>
          </>
        )}

        {view === "transfer" && (
          <>
            <Body className={styles.modalSubtext}>Transaction limit 500</Body>
            <div className={styles.modalBody}>
              <div className={styles.formGroup}>
                <label className={styles.formLabel} htmlFor="transfer-amount">Transaction amount</label>
                <input
                  id="transfer-amount"
                  className={styles.formInput}
                  type="number"
                  value={transferAmount}
                  onChange={(e) => setTransferAmount(e.target.value)}
                  placeholder="Enter amount"
                />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.formLabel} htmlFor="transfer-method">Transfer method</label>
                <select
                  id="transfer-method"
                  className={styles.formSelect}
                  value={transferMethod}
                  onChange={(e) => setTransferMethod(e.target.value)}
                >
                  {TRANSFER_METHODS.map((tm) => (
                    <option key={tm.id} value={tm.id}>{tm.label}</option>
                  ))}
                </select>
              </div>
              <div className={styles.formGroup}>
                <label className={styles.formLabel} htmlFor="transfer-originator">Originator account</label>
                <select
                  id="transfer-originator"
                  className={styles.formSelect}
                  value={transferOriginator}
                  onChange={(e) => setTransferOriginator(e.target.value)}
                >
                  <option value="">Select originator account</option>
                  {accountOptions.map((a) => (
                    <option key={a.id} value={a.id}>{a.label}</option>
                  ))}
                </select>
              </div>
              {renderBeneficiaryField("transfer", transferBeneficiary, setTransferBeneficiary, transferOriginator)}
            </div>
            <div className={styles.modalActions}>
              {error && <Body style={{ color: "#DB3030" }}>{error}</Body>}
              <Button variant="default" onClick={handleClose} disabled={submitting}>Cancel</Button>
              <Button variant="primary" onClick={handleSubmit} disabled={submitting}>
                {submitting ? "Sending…" : "Submit"}
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
