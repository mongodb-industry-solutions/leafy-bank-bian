/**
 * The customer's finished human-review outcomes: executions of this user that
 * were suspended for back-office review and have since been resumed and
 * completed. `result` is the assistant's own message to the customer about
 * the decision (what the resumed run ended with), so the chat can show it
 * without anyone typing. See lib/customer360Oe.js.
 */

import { oeUrl, unreachable } from "@/lib/customer360Oe";

// The engine stores some resumed results double-encoded (UTF-8 bytes read as
// cp1252, e.g. "â€™" for "’"). Undo that when it is clearly present.
const CP1252 = {
  0x20ac: 0x80, 0x201a: 0x82, 0x0192: 0x83, 0x201e: 0x84, 0x2026: 0x85, 0x2020: 0x86, 0x2021: 0x87,
  0x02c6: 0x88, 0x2030: 0x89, 0x0160: 0x8a, 0x2039: 0x8b, 0x0152: 0x8c, 0x017d: 0x8e, 0x2018: 0x91,
  0x2019: 0x92, 0x201c: 0x93, 0x201d: 0x94, 0x2022: 0x95, 0x2013: 0x96, 0x2014: 0x97, 0x02dc: 0x98,
  0x2122: 0x99, 0x0161: 0x9a, 0x203a: 0x9b, 0x0153: 0x9c, 0x017e: 0x9e, 0x0178: 0x9f,
};

function repairMojibake(text) {
  if (!text || !/[âÃ][\u0080-¿€‚-›Œ-Ÿˆ˜™]/.test(text)) return text;
  try {
    const bytes = Uint8Array.from(Array.from(text), (ch) => {
      const cp = ch.codePointAt(0);
      if (cp <= 0xff) return cp;
      if (CP1252[cp] !== undefined) return CP1252[cp];
      throw new Error("not mojibake");
    });
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return text;
  }
}

export async function GET(request) {
  const userId = new URL(request.url).searchParams.get("user_id");
  if (!userId) {
    return Response.json({ success: false, error: "user_id query param is required" }, { status: 400 });
  }

  try {
    const res = await fetch(oeUrl("/query/executions", { user_id: userId, status: "completed", limit: "50" }));
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      return Response.json({ success: false, outcomes: [], error: body?.error || `HTTP ${res.status}` }, { status: res.status });
    }
    const outcomes = (body?.executions || [])
      .filter((e) => e.user_id === userId && Array.isArray(e.suspended_intervals) && e.suspended_intervals.length > 0)
      .map((e) => ({
        execution_id: e.execution_id,
        session_id: e.session_id,
        decision: e.suspended_intervals[e.suspended_intervals.length - 1]?.decision || null,
        result: repairMojibake(e.result || ""),
        updated_at: e.updated_at,
      }));
    return Response.json({ success: true, outcomes });
  } catch (err) {
    return unreachable(err);
  }
}
