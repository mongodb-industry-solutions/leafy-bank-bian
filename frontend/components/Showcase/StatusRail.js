"use client";

// The payment card shared by both walkthroughs: who we are paying, the terms that matter, and
// where the payment stands.

import Badge from "@leafygreen-ui/badge";
import Icon from "@leafygreen-ui/icon";
import { H3, Body } from "@leafygreen-ui/typography";
import styles from "./Showcase.module.css";

const chip = (label, ok) => <Badge key={label} variant={ok ? "green" : "lightgray"}>{label}</Badge>;

function exceptionChip(exceptions) {
  if (!exceptions.length) return null;
  const open = exceptions.filter((e) => e.status === "OPEN");
  if (!open.length) return <Badge variant="green">Exception closed</Badge>;
  const waiting = open.every((e) => e.awaitingCounterparty);
  return (
    <Badge variant={waiting ? "yellow" : "red"}>
      {waiting ? "Awaiting correspondent" : `${open.length} exception${open.length === 1 ? "" : "s"} open`}
    </Badge>
  );
}

const BEARER = {
  DEBT: "Sender pays all fees",
  SHAR: "Fees shared",
  CRED: "Beneficiary pays fees",
  SLEV: "Service-level fees",
};

const maskIban = (iban) => (iban && iban.length > 8 ? `${iban.slice(0, 4)} •••• ${iban.slice(-4)}` : iban);

function Fact({ glyph, children, mono }) {
  return (
    <div className={styles.payFact}>
      <Icon glyph={glyph} size={14} className={styles.payFactIcon} />
      <Body as="span" className={mono ? styles.mono : undefined}>{children}</Body>
    </div>
  );
}

// `chips` replaces the reconciliation chips (the cut-off walkthrough).
// Who we are paying and the terms that matter to reconciliation. Before the wire is sent the
// facts come from the scenario; afterwards the stored payment takes over.
export function PaymentCard({ scenario, init, payment, exceptions, chips }) {
  const amount = Number(init?.amount ?? payment?.amount ?? 0);
  const wire = scenario.wire;
  const creditor = payment?.creditor;
  const name = creditor?.name || wire?.beneficiary;
  const bankName = creditor?.bankName || wire?.bankName;
  const bic = creditor?.bic || wire?.bic;
  const country = creditor?.bankCountry || wire?.country;
  const iban = creditor?.iban || creditor?.accountNo || wire?.iban;
  const bearer = payment?.chargeBearer || init?.chargeBearer || wire?.chargeBearer;
  return (
    <div className={`${styles.railCard} ${styles.toneBorder} ${styles.payCard}`}>
      <H3 as="p" className={styles.paneTitle}>
        {init ? `${payment?.currency || "USD"} ${amount.toLocaleString(undefined, { minimumFractionDigits: 2 })}` : `USD ${scenario.amount}`}
      </H3>
      {wire ? (
        <>
          <Fact glyph="Person">
            <strong>{name}</strong>
            {iban && <span className={styles.muted}> · {maskIban(iban)}</span>}
          </Fact>
          <Fact glyph="Building">{[bankName, bic, country].filter(Boolean).join(" · ")}</Fact>
          {bearer && (
            <Fact glyph="Coin">
              {BEARER[bearer] || bearer} <span className={styles.muted}>({bearer})</span>
            </Fact>
          )}
          <Fact glyph="Note">{wire.purpose}</Fact>
        </>
      ) : (
        <Body className={styles.muted}>{scenario.bank} · {scenario.title}</Body>
      )}
      {init ? (
        <Fact glyph="Link" mono>{init.paymentId}</Fact>
      ) : (
        <Fact glyph="Clock">Not sent yet</Fact>
      )}
      {init && chips && <div className={styles.chipRow}>{chips}</div>}
      {init && !chips && (
        <div className={styles.chipRow}>
          {chip("Settled", ["SETTLED", "COMPLETED"].includes(payment?.status))}
          {chip("Booked", payment?.lifecycle?.postingStatus === "POSTED")}
          {chip("Reconciled", payment?.lifecycle?.reconciliationStatus === "RECONCILED")}
          {exceptionChip(exceptions)}
        </div>
      )}
    </div>
  );
}
