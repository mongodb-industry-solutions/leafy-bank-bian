"use client";

import { useBankLogin } from "@/lib/api/useBankLogin";
import Banner from "@leafygreen-ui/banner";
import Button from "@leafygreen-ui/button";
import Card from "@leafygreen-ui/card";
import { Body, H2, Subtitle } from "@leafygreen-ui/typography";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import styles from "./page.module.css";

/* ── Permission display helpers ── */
const PERMISSION_META = {
  LOANS_READ: { icon: "🏦", label: "Loans", access: "Read Only" },
  ACCOUNTS_READ: { icon: "💳", label: "Accounts", access: "Read Only" },
  ACCOUNTS_BALANCES_READ: {
    icon: "💰",
    label: "Account Balances",
    access: "Read Only",
  },
  REPAYMENT_HISTORY_READ: {
    icon: "📋",
    label: "Repayment History",
    access: "Read Only",
  },
  CUSTOMER_IDENTIFICATION_READ: {
    icon: "🪪",
    label: "Identity",
    access: "Read Only",
  },
  TRANSACTIONS_READ: { icon: "💳", label: "Transactions", access: "Read Only" },
};

function friendlyPermission(raw) {
  const meta = PERMISSION_META[raw];
  if (meta) return meta;
  // Fallback: de-snake-case
  const label = raw.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  return { icon: "📄", label, access: "Read Only" };
}

function friendlyPurpose(raw) {
  if (!raw) return raw;
  return raw
    .replace(/_/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function friendlyDate(raw) {
  if (!raw) return raw;
  try {
    const d = new Date(raw);
    return d.toLocaleDateString("en-US", {
      month: "long",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return raw;
  }
}

export default function BankLoginPage() {
  return (
    <Suspense
      fallback={
        <div className={styles.container}>
          <Body>Loading...</Body>
        </div>
      }
    >
      <BankLoginContent />
    </Suspense>
  );
}

function BankLoginContent() {
  const searchParams = useSearchParams();

  // Extract URL params (routing concern)
  const consentId = searchParams.get("consent_id");
  const institutionName = searchParams.get("institution_name");
  const threadId = searchParams.get("thread_id");

  const {
    step,
    token,
    tokenError,
    consentData,
    statusText,
    isValid,
    handleBankLogin,
    handleConsentDecision,
    handleCloseTab,
    encryptionDemo,
    encryptionLoading,
  } = useBankLogin({ consentId, institutionName, threadId });

  // Guard: no params or no user
  if (!isValid) {
    return (
      <main className={styles.container}>
        <Card className={styles.card}>
          <H2>Invalid Request</H2>
          <Body>
            Missing required parameters. Please use the chatbot to initiate a
            bank connection.
          </Body>
          <Button
            variant="primary"
            onClick={() => {
              window.location.href = "/";
            }}
            style={{ marginTop: 16 }}
          >
            Return Home
          </Button>
        </Card>
      </main>
    );
  }

  return (
    <main className={styles.container}>
      <Card className={styles.card}>
        {/* Header */}
        <div className={styles.header}>
          <H2>{institutionName} — Bank Login</H2>
          <Body className={styles.subtitle}>
            Consent ID: {consentId.substring(0, 30)}...
          </Body>
        </div>

        {/* Step: Login */}
        {step === "login" && (
          <div className={styles.section}>
            {tokenError ? (
              <div className={styles.error}>
                <Body>{tokenError}</Body>
              </div>
            ) : token ? (
              <>
                <div className={styles.tokenBox}>
                  <Subtitle>Authorization Token</Subtitle>
                  <code className={styles.tokenValue}>
                    {token.substring(0, 16)}...
                    {token.substring(token.length - 8)}
                  </code>
                  <Body className={styles.tokenHint}>
                    Token auto-retrieved from {institutionName}
                  </Body>
                </div>
                <Button
                  variant="primary"
                  onClick={handleBankLogin}
                  className={styles.actionBtn}
                >
                  Log in to {institutionName}
                </Button>
              </>
            ) : null}
          </div>
        )}

        {/* Step: Fetching token */}
        {step === "fetching" && (
          <div className={styles.section}>
            <div className={styles.spinner} />
            <Body>{statusText}</Body>
          </div>
        )}

        {/* Step: Processing */}
        {step === "processing" && (
          <div className={styles.section}>
            <div className={styles.spinner} />
            <Body>{statusText}</Body>
          </div>
        )}

        {/* Step: Consent Approval */}
        {step === "consent" && consentData && (
          <div className={styles.section}>
            <div className={styles.consentBox}>
              <Subtitle>Consent Approval Required</Subtitle>
              <Body className={styles.consentMessage}>
                {consentData.message ||
                  "Do you approve this data-sharing consent?"}
              </Body>

              {consentData.permissions && (
                <div className={styles.consentSection}>
                  <div className={styles.consentSectionHeader}>
                    <span className={styles.consentSectionIcon}>📋</span>
                    <Body weight="medium">
                      Permissions ({consentData.permissions.length})
                    </Body>
                  </div>
                  <div className={styles.permissionList}>
                    {consentData.permissions.map((perm) => {
                      const { icon, label, access } = friendlyPermission(perm);
                      return (
                        <div key={perm} className={styles.permissionRow}>
                          <span className={styles.permissionIcon}>{icon}</span>
                          <span className={styles.permissionLabel}>
                            {label}
                          </span>
                          <span className={styles.permissionBadge}>
                            {access}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {consentData.purpose && (
                <div className={styles.consentSection}>
                  <div className={styles.consentSectionHeader}>
                    <span className={styles.consentSectionIcon}>🎯</span>
                    <Body weight="medium">Purpose</Body>
                  </div>
                  <Body className={styles.consentSectionValue}>
                    {friendlyPurpose(consentData.purpose)}
                  </Body>
                </div>
              )}

              {consentData.source_institution && (
                <div className={styles.consentSection}>
                  <div className={styles.consentSectionHeader}>
                    <span className={styles.consentSectionIcon}>🏛️</span>
                    <Body weight="medium">Source Institution</Body>
                  </div>
                  <Body className={styles.consentSectionValue}>
                    {consentData.source_institution}
                  </Body>
                </div>
              )}

              {(consentData.display_expiration || consentData.expiration) && (
                <div className={styles.consentSection}>
                  <div className={styles.consentSectionHeader}>
                    <span className={styles.consentSectionIcon}>⏱️</span>
                    <Body weight="medium">Expires</Body>
                  </div>
                  <Body className={styles.consentSectionValue}>
                    {friendlyDate(
                      consentData.display_expiration || consentData.expiration,
                    )}
                  </Body>
                </div>
              )}
            </div>

            {encryptionLoading && (
              <p className={styles.encryptionLoading}>
                Verifying encryption...
              </p>
            )}
            {encryptionDemo?.encryption_verified && (
              <div className={styles.encryptionProof}>
                <Banner variant="info">
                  <strong>Your data is encrypted in use 🔐</strong>
                  This means your sensitive information is securely encrypted
                  before being stored, and only decrypted when accessed with
                  proper authorization.
                  <br />
                  <br />
                  Powered by MongoDB Queryable Encryption, which allows data to
                  remain encrypted even while being queried — ensuring maximum
                  privacy and security.
                </Banner>
                <table className={styles.encryptionTable}>
                  <thead>
                    <tr>
                      <th>Field</th>
                      <th>App sees</th>
                      <th>On disk</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(encryptionDemo.encrypted_fields).map(
                      ([field, info]) => (
                        <tr key={field}>
                          <td>{field.split(".").pop()}</td>
                          <td>
                            {Array.isArray(info.decrypted_value)
                              ? `[${info.decrypted_value.length} items]`
                              : String(info.decrypted_value)}
                          </td>
                          <td>
                            <span className={styles.encryptedBadge}>
                              {info.is_encrypted
                                ? "🔒 Encrypted Binary"
                                : info.raw_type}
                            </span>
                          </td>
                        </tr>
                      ),
                    )}
                  </tbody>
                </table>
              </div>
            )}

            <div className={styles.actions}>
              <Button
                variant="primary"
                onClick={() => handleConsentDecision(true)}
              >
                Approve Consent
              </Button>
              <Button
                variant="default"
                onClick={() => handleConsentDecision(false)}
              >
                Decline
              </Button>
            </div>
          </div>
        )}

        {/* Step: Done */}
        {step === "done" && (
          <div className={styles.section}>
            <div className={styles.doneBox}>
              <Subtitle>{statusText}</Subtitle>
              <Body>
                Your data has been sent back to the chatbot. You can close this
                tab.
              </Body>
            </div>
            <Button
              variant="primary"
              onClick={handleCloseTab}
              className={styles.actionBtn}
            >
              Close This Tab
            </Button>
          </div>
        )}
      </Card>
    </main>
  );
}
