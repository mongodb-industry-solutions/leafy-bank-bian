/**
 * Polls an execution's status/result straight from the orchestration engine —
 * resume is asynchronous ("status": "resuming"), so the back-office UI polls
 * this after /api/customer360-review/resume until the run is terminal.
 * See lib/customer360Oe.js.
 */

import { oeUrl, unreachable } from "@/lib/customer360Oe";

export async function GET(request) {
  const executionId = new URL(request.url).searchParams.get("execution_id");
  if (!executionId) {
    return Response.json({ success: false, error: "execution_id query param is required" }, { status: 400 });
  }

  try {
    const res = await fetch(oeUrl(`/query/executions/${encodeURIComponent(executionId)}`));
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      return Response.json({ success: false, error: body?.error || `HTTP ${res.status}` }, { status: res.status });
    }
    const execution = body?.execution || null;
    // status/result are also surfaced at the top level: the back-office page
    // polls `data.status` until it leaves "resuming".
    return Response.json({ success: true, execution, status: execution?.status, result: execution?.result });
  } catch (err) {
    return unreachable(err);
  }
}
