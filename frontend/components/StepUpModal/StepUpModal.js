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

/**
 * @param {boolean} open
 * @param {string}  reason    the refusal text from the backend, shown verbatim
 * @param {Function} onCancel
 * @param {Function} onSuccess called once the session is re-issued at two factors; the
 *                             caller retries its original request
 */
export default function StepUpModal({ open, reason, onCancel, onSuccess }) {
  const [challenge, setChallenge] = useState(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

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
    <Modal open={open} setOpen={(v) => !v && onCancel?.()}>
      <H3>Additional authentication required</H3>

      {reason && (
        <Banner variant="warning" style={{ marginTop: 12 }}>
          {reason}
        </Banner>
      )}

      <Body style={{ marginTop: 16 }}>
        This payment is above the entitlement step-up threshold for the account&apos;s
        customer segment, so a second factor is required before it can be initiated.
      </Body>

      <Banner variant="info" style={{ marginTop: 16 }}>
        <strong>SIMULATED</strong> — a real deployment sends this code to the customer&apos;s
        registered device. Here it is shown on screen, so this demonstrates the step-up
        control, not two-factor authentication. The code is bound to this session and is
        verified server-side: a wrong code is refused.
      </Banner>

      {challenge?.challengeCode && (
        <Body style={{ marginTop: 16 }}>
          One-time code: <strong style={{ letterSpacing: 2 }}>{challenge.challengeCode}</strong>
        </Body>
      )}

      <TextInput
        label="One-time code"
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
