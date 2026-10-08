import { agentApi, coreApi } from "@/lib/api/client";
import { STAFF } from "./cutoffScenarios";

// Every call returns { data, error } (the client helpers' shape); the stepper decides what
// an error means. 409 is never retried automatically: the presenter tries again.

export const startCutoffScenario = (scenario) =>
  coreApi("workflow/demo/cutoff-scenario", { method: "POST", body: { scenario } });

/** Anchor the run's business clock to HH:MM ET. Forward only; the backend 400s on backwards. */
export const moveClock = (runId, anchor) =>
  coreApi("workflow/demo/clock", { method: "POST", body: { runId, anchor } });

/**
 * Fast-forward the run to the next business day and close its held payments. Safe to repeat.
 * Resolves with `{ clock, payments: [{ paymentId, before, action, after, error? }] }`.
 */
export async function fastForward(runId) {
  const { data, error } = await coreApi("workflow/demo/cutoff-release", { method: "POST", body: { runId } });
  if (error && String(error).startsWith("409")) return { data, error: "Busy, try again in a moment" };
  return { data, error };
}

export const approveAsRaj = (paymentId) =>
  coreApi("PaymentOrderProcedure/Approve", {
    method: "POST",
    body: { paymentId, approverId: STAFF.RAJ, decision: "APPROVED" },
  });

/**
 * One sweep for this payment. The backend runs the LLM inline, so this resolves tens of
 * seconds later: callers must not await it for progress (the case poll shows that).
 */
export const sweepNow = (paymentId) =>
  agentApi("cutoff/sweep", null, { method: "POST", body: { paymentId } });

export const decideCutoff = (caseId, decision) =>
  agentApi(`cutoff/cases/${encodeURIComponent(caseId)}/approve`, null, {
    method: "POST",
    body: { decision, by: "presenter" },
  });

/** The newest case for a payment, or null. */
export async function findCase(paymentId) {
  const { data, error } = await agentApi("cutoff/cases", { paymentId });
  return { data: data?.cases?.[0] || null, error };
}

/** Backend errors in words a presenter can act on. */
export function cutoffError(err) {
  const s = String(err);
  if (s.startsWith("409")) return "Busy, try again in a moment.";
  if (s.startsWith("503")) return "Cut-off agent not running.";
  if (s.startsWith("400") && s.includes("before the run's business time")) return "The clock is already past that time.";
  return s;
}
