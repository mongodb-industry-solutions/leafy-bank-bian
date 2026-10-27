/**
 * Direct access to the customer360-agent's orchestration engine (OE) for the
 * back-office human-review flow — the same OE the chat already calls
 * (CUSTOMER360_AGENT_URL, see app/api/customer360-chat/route.js). It does not
 * go through the `agentic dev` playground UI, which only proxies these same
 * OE endpoints (/query/executions, /resume/{id}) and adds the org/project.
 *
 * The OE requires org_id and project_id on every query/resume call (an
 * execution id alone must not authorize anything). Defaults are the local
 * `agentic dev up` stack's ids; override both for any other environment.
 */

export const OE_BASE = process.env.CUSTOMER360_AGENT_URL || "http://localhost:8000";
export const ORG_ID = process.env.CUSTOMER360_ORG_ID || "000000000000000000000001";
export const PROJECT_ID = process.env.CUSTOMER360_PROJECT_ID || "d5b2d2232df435ae2dac25a1";

export function oeUrl(path, extra = {}) {
  const qs = new URLSearchParams({ org_id: ORG_ID, project_id: PROJECT_ID, ...extra });
  return `${OE_BASE}${path}?${qs.toString()}`;
}

export function unreachable(err) {
  return Response.json(
    { success: false, error: `Orchestration engine unreachable at ${OE_BASE} (${err?.cause?.code || err?.message || "fetch failed"})` },
    { status: 502 },
  );
}
