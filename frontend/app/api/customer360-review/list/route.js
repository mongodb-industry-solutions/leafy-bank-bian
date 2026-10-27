/**
 * Lists suspended executions (pending back-office reviews) straight from the
 * customer360-agent's orchestration engine — see lib/customer360Oe.js.
 */

import { oeUrl, unreachable } from "@/lib/customer360Oe";

export async function GET() {
  try {
    const res = await fetch(oeUrl("/query/executions", { status: "suspended" }));
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      return Response.json(
        { success: false, executions: [], count: 0, error: body?.error || `HTTP ${res.status}` },
        { status: res.status },
      );
    }
    const executions = body?.executions || [];
    return Response.json({ success: true, executions, count: body?.count ?? executions.length });
  } catch (err) {
    return unreachable(err);
  }
}
