/**
 * Polls a resumed execution's status/result via the local `agentic dev up`
 * UI proxy — resume is asynchronous ("status": "resuming"), so the
 * back-office UI polls this after calling /api/customer360-review/resume
 * until the run reaches a terminal status. Same local base as resume/
 * list — no credentials needed for a local dev stack.
 */

const CUSTOMER360_LOCAL_UI_BASE = process.env.CUSTOMER360_LOCAL_UI_BASE || "http://localhost:3000";

export async function GET(request) {
  const executionId = new URL(request.url).searchParams.get("execution_id");
  if (!executionId) {
    return Response.json({ success: false, error: "execution_id query param is required" }, { status: 400 });
  }

  const backendRes = await fetch(
    `${CUSTOMER360_LOCAL_UI_BASE}/api/v1/executions/${encodeURIComponent(executionId)}`,
  );

  const text = await backendRes.text();
  return new Response(text, {
    status: backendRes.status,
    headers: { "Content-Type": backendRes.headers.get("Content-Type") || "application/json" },
  });
}
