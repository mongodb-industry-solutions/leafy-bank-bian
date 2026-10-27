/**
 * Resumes a suspended human-review execution with the reviewer's decision,
 * straight against the orchestration engine's /resume/{id} — see
 * lib/customer360Oe.js. Only for app.suspend() reviews (loans/insurance); the
 * engine feeds the decision back to the agent as the suspended tool's result.
 * Resume is asynchronous: it returns {status: "resuming"} immediately.
 */

import { oeUrl, unreachable } from "@/lib/customer360Oe";

export async function POST(request) {
  const { execution_id, decision, reviewer_notes } = await request.json();
  if (!execution_id || !decision) {
    return Response.json({ success: false, error: "execution_id and decision are required" }, { status: 400 });
  }
  if (!["approved", "denied"].includes(decision)) {
    return Response.json({ success: false, error: "decision must be 'approved' or 'denied'" }, { status: 400 });
  }

  try {
    const res = await fetch(oeUrl(`/resume/${encodeURIComponent(execution_id)}`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ human_review: { decision, reviewer_notes: reviewer_notes || "" } }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      return Response.json(
        { success: false, execution_id, status: "error", error: body?.error || `HTTP ${res.status}` },
        { status: res.status },
      );
    }
    return Response.json({ success: true, execution_id: body?.execution_id || execution_id, status: body?.status || "resuming" });
  } catch (err) {
    return unreachable(err);
  }
}
