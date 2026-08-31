// BIAN PartyAuthentication (SD 38917) — the step-up factor.
//
// Stage 2's entitlement policy refuses a single weak factor above the segment's step-up
// threshold (COMMERCIAL at 10,000). Level 1 issues PASSWORD/1, so a payment over that
// threshold is refused with "step-up authentication required" — correct, and until now
// unanswerable from the UI. These two calls answer it:
//
//   Question/{id}/Retrieve  — fetch the challenge (the code, because this is a simulation)
//   Question/Evaluate       — submit it; on success the session is re-issued at OTP/2
//
// The step-up happens BECAUSE the entitlement engine demanded it. That ordering is the
// demo beat, and it is why the refusal is not simply configured away.

import { coreApi, currentSessionRef, setSessionToken } from "@/lib/api/client";

// The transactions service's refusal text for an insufficient factor
// (`authenticate.py`: "… is not sufficient for … — step-up authentication required.").
// Matched on the stable phrase, not the whole string, which carries a formatted amount.
const STEP_UP_MARKER = "step-up authentication required";

/** Does this initiate error mean "collect a second factor and retry"? */
export function isStepUpRequired(error) {
  return typeof error === "string" && error.toLowerCase().includes(STEP_UP_MARKER);
}

/**
 * The challenge for the live session.
 *
 * Returns `{ challengeCode, questionText, simulated }` — the code included, which a real
 * bank would never do (it would go to a registered device out of band). The backend marks
 * it `deliveryChannel: ON_SCREEN_SIMULATION`; surface that wherever it is displayed.
 */
export async function retrieveStepUpChallenge() {
  const ref = currentSessionRef();
  if (!ref) return { data: null, error: "No authenticated session to step up." };

  return coreApi(`PartyAuthentication/${ref}/Question/otp/Retrieve`);
}

/**
 * Submit the challenge answer. On success the module-scope session token is REPLACED with
 * the two-factor one, so the caller only has to retry its original request.
 *
 * A wrong code is a real 401 — the code is an HMAC over the session ref, so it is verified,
 * not accepted. That is what makes a failed step-up demonstrable.
 */
export async function evaluateStepUpChallenge(challengeResponse) {
  const ref = currentSessionRef();
  if (!ref) return { data: null, error: "No authenticated session to step up." };

  const { data, error } = await coreApi(`PartyAuthentication/${ref}/Question/Evaluate`, {
    method: "POST",
    body: { challengeResponse, questionId: "otp" },
  });
  if (error) return { data: null, error };

  // A NEW session ref, not the old one strengthened: the assessment is immutable and is
  // not persisted, so the stronger one is a fresh issue. The payment records whichever
  // session it was actually initiated under.
  setSessionToken(data?.accessToken ?? null, data?.partyAuthenticationId ?? null);
  return { data: data?.assessment ?? null, error: null };
}
