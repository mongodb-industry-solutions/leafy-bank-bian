/**
 * Resumes a suspended legacy human-review execution via the local
 * `agentic dev up` UI proxy (http://localhost:3000/api/v1/executions/
 * {execution_id}/resume) — NOT the hosted API Gateway. For a local dev
 * stack there's no project/workspace/API-key: the local UI adds the local
 * synthetic org/project scope itself, so this route needs no credentials
 * (unlike a real deployment, which would use the Gateway contract with
 * Authorization/project_id/workspace_id instead).
 *
 * Only for legacy human-review/guardrail suspensions (human_review.py uses
 * app.suspend(), not langgraph.types.interrupt) — native interrupt
 * suspensions resume through /invoke's resume_map instead.
 */

const CUSTOMER360_LOCAL_UI_BASE = process.env.CUSTOMER360_LOCAL_UI_BASE || "http://localhost:3000";

export async function POST(request) {
  const { execution_id, decision, reviewer_notes } = await request.json();
  if (!execution_id || !decision) {
    return Response.json({ success: false, error: "execution_id and decision are required" }, { status: 400 });
  }

  const backendRes = await fetch(
    `${CUSTOMER360_LOCAL_UI_BASE}/api/v1/executions/${encodeURIComponent(execution_id)}/resume`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, reviewer_notes: reviewer_notes || "" }),
    },
  );

  const text = await backendRes.text();
  return new Response(text, {
    status: backendRes.status,
    headers: { "Content-Type": backendRes.headers.get("Content-Type") || "application/json" },
  });
}
