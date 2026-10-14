/**
 * Lists suspended executions via the local `agentic dev up` UI proxy — see
 * resume/route.js's docstring for why this is a different base than the
 * hosted API Gateway (and requires no credentials): the local UI adds the
 * local synthetic org/project scope itself.
 */

const CUSTOMER360_LOCAL_UI_BASE = process.env.CUSTOMER360_LOCAL_UI_BASE || "http://localhost:3000";

export async function GET() {
  const backendRes = await fetch(`${CUSTOMER360_LOCAL_UI_BASE}/api/v1/executions?status=suspended`);

  const text = await backendRes.text();
  return new Response(text, {
    status: backendRes.status,
    headers: { "Content-Type": backendRes.headers.get("Content-Type") || "application/json" },
  });
}
