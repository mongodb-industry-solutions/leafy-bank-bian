import { agentApi } from "@/lib/api/client";

/** Backend errors in words a presenter can act on. */
export function friendlyError(err) {
  const s = String(err);
  if (s.startsWith("409")) return "Nothing awaiting approval.";
  if (s.startsWith("503")) return "Agent not running.";
  if (s.startsWith("502")) return `Resume failed — ${s}`;
  return s;
}

export const decideProposal = (exceptionId, decision) =>
  agentApi(`reconciliation/${encodeURIComponent(exceptionId)}/approve`, null, {
    method: "POST",
    body: { decision, by: "presenter" },
  });

// The worker also retries every minute; this lets the presenter retry right after re-login.
export const retryInvestigation = (exceptionId, paymentId) =>
  agentApi("reconciliation/investigate", null, { method: "POST", body: { exceptionId, paymentId } });
