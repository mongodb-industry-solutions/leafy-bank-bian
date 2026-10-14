/**
 * Proxy to the customer360-agent Magenta app's orchestration-engine
 * endpoint (POST /invoke) — synchronous, single JSON response, no
 * streaming. See magenta-customer360-agents/agents/customer360-agent.
 */

const CUSTOMER360_AGENT_URL =
  process.env.CUSTOMER360_AGENT_URL || "http://localhost:8000";

export async function POST(request) {
  const body = await request.text();

  let backendRes;
  try {
    backendRes = await fetch(`${CUSTOMER360_AGENT_URL}/invoke`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  } catch (e) {
    // Without this, a connection failure (e.g. nothing listening at
    // CUSTOMER360_AGENT_URL) throws inside the route handler and Next.js
    // returns a bare 500 with no body — indistinguishable from the agent
    // itself erroring. Surface which URL failed and why instead.
    return Response.json(
      { success: false, error: `Could not reach ${CUSTOMER360_AGENT_URL}/invoke: ${e.message}` },
      { status: 502 },
    );
  }

  const text = await backendRes.text();
  return new Response(text, {
    status: backendRes.status,
    headers: {
      "Content-Type": backendRes.headers.get("Content-Type") || "application/json",
    },
  });
}
