"use client";

// The step-up challenge, shown when stage 2 refuses a payment for an insufficient factor.
//
// Reusable on purpose: both initiation surfaces hit the same refusal — the back-office
// Create Payment form (COMMERCIAL threshold 10,000) and the retail Send Money modal
// (RETAIL threshold 2,500, which `AUTOFILL_MAX` of 20,000 now clears routinely).
//
// It states that it is a simulation, in the UI and not only in a code comment. The code is
// displayed on the same screen that answers it, so there is no out-of-band channel and this
// is not two-factor authentication. What is real: the code is an HMAC bound to this session,
// the server verifies it, and a wrong answer is refused.
//
// The `reason` prop is the backend's REFUSAL string, not a message meant for a user — it
// carries the HTTP status and a Python repr (`400: {'detail': …}`). We never show it
// verbatim; we extract the amount it wraps and render clean prose instead, so the panel
// reads like a bank's auth step rather than a leaked API response.

import React, { useEffect, useState } from "react";
import Modal from "@leafygreen-ui/modal";
import Button from "@leafygreen-ui/button";
import Banner from "@leafygreen-ui/banner";
import TextInput from "@leafygreen-ui/text-input";
import { Body, H3 } from "@leafygreen-ui/typography";
import {
  evaluateStepUpChallenge,
  retrieveStepUpChallenge,
} from "@/lib/api/partyAuthentication";
import { fmtAmount } from "@/lib/paymentsWorkflow/status";
import styles from "./StepUpModal.module.css";

// The backend refusal's own phrasing (`authenticate.py`): "… is not sufficient for
// 11,308.66 — step-up authentication required." We match on the amount it embeds, never on
// the status/repr wrapper, so a change upstream to the HTTP shape cannot break the copy.
const AMOUNT_RE = /sufficient for ([\d,]+\.\d{2})/;

/** The payment amount the backend refused, parsed from the refusal string. */
function amountFromReason(reason) {
  if (typeof reason !== "string") return null;
  const m = reason.match(AMOUNT_RE);
  if (!m) return null;
  const amount = Number(m[1].replace(/,/g, ""));
  return Number.isNaN(amount) ? null : amount;
}

/**
 * @param {boolean} open
 * @param {string}  reason    the backend refusal string — parsed, never shown raw
 * @param {Function} onCancel
 * @param {Function} onSuccess called once the session is re-issued at two factors; the
 *                             caller retries its original request
 */
export default function StepUpModal({ open, reason, onCancel, onSuccess }) {
  const [challenge, setChallenge] = useState(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const amount = amountFromReason(reason);

  useEffect(() => {
    if (!open) {
      setChallenge(null);
      setCode("");
      setError(null);
      return;
    }
    let live = true;
    retrieveStepUpChallenge().then(({ data, error: e }) => {
      if (!live) return;
      if (e) setError(e);
      else setChallenge(data);
    });
    return () => {
      live = false;
    };
  }, [open]);

  async function confirm() {
    setBusy(true);
    setError(null);
    const { error: e } = await evaluateStepUpChallenge(code.trim());
    setBusy(false);
    if (e) {
      // Left open deliberately: a wrong code is a retry, not a dead end.
      setError(e);
      return;
    }
    onSuccess?.();
  }

  return (
    <Modal open={open} setOpen={(v) => !v && onCancel?.()} contentClassName={styles.content}>
      <H3>Additional authentication required</H3>

      <Body style={{ marginTop: 12 }}>
        {amount != null
          ? `This payment of ${fmtAmount(amount)} is above the step-up threshold for this account, so a second factor is required before it can be initiated.`
          : "This payment is above the step-up threshold for this account, so a second factor is required before it can be initiated."}
      </Body>

      <Banner variant="info" style={{ marginTop: 16 }}>
        <strong>Simulated:</strong> in production this code goes to the customer&apos;s
        registered device. It is shown on screen here, so this demonstrates the step-up
        control, not two-factor authentication. A wrong code is refused.
      </Banner>

      {challenge?.challengeCode && (
        <div style={{ marginTop: 20 }}>
          <Body style={{ fontWeight: 600 }}>Verification code</Body>
          <div
            style={{
              marginTop: 8,
              padding: "16px 14px",
              borderRadius: 8,
              background: "#F9FBFA",
              border: "1px solid #E8EDEB",
              fontFamily: "'SFMono-Regular', 'MongoDB Value Serif', Menlo, monospace",
              fontSize: 26,
              fontWeight: 600,
              letterSpacing: 6,
              textAlign: "center",
              color: "#001E2B",
            }}
          >
            {challenge.challengeCode}
          </div>
        </div>
      )}

      <TextInput
        label="Enter the one-time code"
        description={challenge?.questionText || "Enter the six-digit code."}
        value={code}
        onChange={(e) => setCode(e.target.value)}
        state={error ? "error" : "none"}
        errorMessage={error || undefined}
        style={{ marginTop: 16 }}
      />

      <div style={{ display: "flex", gap: 8, marginTop: 24, justifyContent: "flex-end" }}>
        <Button onClick={() => onCancel?.()} disabled={busy}>
          Cancel
        </Button>
        <Button
          variant="primary"
          onClick={confirm}
          disabled={busy || code.trim().length === 0}
        >
          {busy ? "Verifying…" : "Verify and continue"}
        </Button>
      </div>
    </Modal>
  );
}
