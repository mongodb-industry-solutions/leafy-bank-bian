const CORE_BASE = "/api/backend";
const CHATBOT_BASE = "/api/chatbot";
const OPENFINANCE_CHAT_BASE = "/api/openfinance-chat";
const CUSTOMER360_CHAT_BASE = "/api/customer360-chat";

/**
 * Core backend API client.
 * @param {string} path - path after /api/v1/, e.g. "leafybank/accounts/secure/fetch-accounts-for-user"
 * @param {object} options
 * @param {string} [options.method="GET"]
 * @param {object} [options.body]
 * @param {string} [options.bearerToken]
 * @param {object} [options.params] - query params as key-value pairs (keeps path and query separate to avoid URL normalization issues)
 * @returns {Promise<{data: any, error: string|null}>}
 */
export async function coreApi(path, { method = "GET", body = null, bearerToken = null, params = null } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (bearerToken) {
    headers["Authorization"] = `Bearer ${bearerToken}`;
  }

  let url = `${CORE_BASE}/${path}`;
  if (params) {
    const qs = new URLSearchParams(params).toString();
    url += (url.includes("?") ? "&" : "?") + qs;
  }

  try {
    const res = await fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : null,
    });

    if (!res.ok) {
      const errText = await res.text();
      return { data: null, error: `${res.status}: ${errText}` };
    }

    const data = await res.json();
    return { data, error: null };
  } catch (e) {
    return { data: null, error: e.message };
  }
}

/**
 * GL pipeline monitor client (ledger service via the proxy). GET by default;
 * pass options.method/body for the manual batch trigger (POST /pipeline/batch/trigger).
 * @param {string} path - path after the prefix, e.g. "trace/PAY-123" or "health"
 * @param {object} [params] - query params as key-value pairs (kept separate from path to avoid URL normalization issues)
 * @param {object} [options]
 * @param {string} [options.method="GET"]
 * @param {object} [options.body]
 * @returns {Promise<{data: any, error: string|null}>}
 */
export async function pipelineApi(path, params = null, { method = "GET", body = null } = {}) {
  let url = `${CORE_BASE}/pipeline/${path}`;
  if (params) {
    const clean = Object.fromEntries(
      Object.entries(params).filter(([, v]) => v !== null && v !== undefined)
    );
    const qs = new URLSearchParams(clean).toString();
    if (qs) url += (url.includes("?") ? "&" : "?") + qs;
  }

  try {
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : null,
    });

    if (!res.ok) {
      const errText = await res.text();
      return { data: null, error: `${res.status}: ${errText}` };
    }

    return { data: await res.json(), error: null };
  } catch (e) {
    return { data: null, error: e.message };
  }
}

/**
 * Chatbot backend API client (non-streaming).
 * @param {string} path - path after root, e.g. "chat"
 * @param {object} options
 * @returns {Promise<{data: any, error: string|null}>}
 */
export async function chatApi(path, { method = "POST", body = null } = {}) {
  const headers = { "Content-Type": "application/json" };

  try {
    const res = await fetch(`${CHATBOT_BASE}/${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : null,
    });

    if (!res.ok) {
      const errText = await res.text();
      return { data: null, error: `${res.status}: ${errText}` };
    }

    const data = await res.json();
    return { data, error: null };
  } catch (e) {
    return { data: null, error: e.message };
  }
}

/**
 * Chatbot streaming — returns the raw Response for SSE processing.
 * @param {string} path - e.g. "chat/stream"
 * @param {object} body - request body
 * @returns {Promise<Response>}
 */
export async function chatStream(path, body) {
  const res = await fetch(`${CHATBOT_BASE}/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    throw new Error(`${res.status}: ${await res.text()}`);
  }

  return res;
}

/**
 * Open Banking react-agent chatbot streaming — returns the raw Response for SSE.
 * Use this (not chatStream) for the consent flow: the consent thread lives on the
 * Open Banking chatbot, so its /chat/stream/resume must hit the same backend.
 * @param {string} path - e.g. "chat/stream/resume"
 * @param {object} body - request body
 * @returns {Promise<Response>}
 */
export async function openFinanceChatStream(path, body) {
  const res = await fetch(`${OPENFINANCE_CHAT_BASE}/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    throw new Error(`${res.status}: ${await res.text()}`);
  }

  return res;
}

/**
 * Open Banking react-agent chatbot client (non-streaming).
 * @param {string} path - e.g. "chat"
 * @param {object} [options]
 * @param {object} [options.body]
 * @returns {Promise<{data: any, error: string|null}>}
 */
export async function openFinanceChatApi(path, { body = null } = {}) {
  try {
    const res = await fetch(`${OPENFINANCE_CHAT_BASE}/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : null,
    });

    if (!res.ok) {
      const errText = await res.text();
      return { data: null, error: `${res.status}: ${errText}` };
    }

    return { data: await res.json(), error: null };
  } catch (e) {
    return { data: null, error: e.message };
  }
}

/**
 * customer360-agent (Magenta) client — synchronous, single JSON response.
 * Body shape: { message, session_id, user_id }. Response shape (confirmed
 * against a live local `agentic dev up` call — the hosted-platform docs'
 * { success, response, ... } shape does NOT apply locally):
 * { result, session_id, user_id, execution_id, status }. No `success`
 * field locally; `status` ("completed"/"suspended"/...) is the only
 * outcome signal, and the text is under `result`, not `response`.
 * @param {object} body
 * @returns {Promise<{data: any, error: string|null}>}
 */
export async function customer360ChatApi(body) {
  try {
    const res = await fetch(CUSTOMER360_CHAT_BASE, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const errText = await res.text();
      return { data: null, error: `${res.status}: ${errText}` };
    }

    return { data: await res.json(), error: null };
  } catch (e) {
    return { data: null, error: e.message };
  }
}

/**
 * Resume a suspended legacy human-review execution (see
 * app/api/customer360-review/resume/route.js). Async on the platform side —
 * returns {success, execution_id, status: "resuming"} immediately; poll
 * getCustomer360ExecutionStatus for the final result.
 * @param {{executionId: string, decision: string, reviewerNotes?: string}} args
 * @returns {Promise<{data: any, error: string|null}>}
 */
export async function resumeCustomer360Review({ executionId, decision, reviewerNotes }) {
  try {
    const res = await fetch("/api/customer360-review/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ execution_id: executionId, decision, reviewer_notes: reviewerNotes || "" }),
    });

    if (!res.ok) {
      const errText = await res.text();
      return { data: null, error: `${res.status}: ${errText}` };
    }

    return { data: await res.json(), error: null };
  } catch (e) {
    return { data: null, error: e.message };
  }
}

/**
 * Poll a resumed execution's status/result (see
 * app/api/customer360-review/status/route.js).
 * @param {string} executionId
 * @returns {Promise<{data: any, error: string|null}>}
 */
export async function getCustomer360ExecutionStatus(executionId) {
  try {
    const res = await fetch(`/api/customer360-review/status?execution_id=${encodeURIComponent(executionId)}`);

    if (!res.ok) {
      const errText = await res.text();
      return { data: null, error: `${res.status}: ${errText}` };
    }

    return { data: await res.json(), error: null };
  } catch (e) {
    return { data: null, error: e.message };
  }
}

/**
 * List currently suspended executions (see
 * app/api/customer360-review/list/route.js) — the local `agentic dev up`
 * stack's own record, not a client-side queue.
 * @returns {Promise<{data: any, error: string|null}>}
 */
export async function listCustomer360SuspendedReviews() {
  try {
    const res = await fetch("/api/customer360-review/list");

    if (!res.ok) {
      const errText = await res.text();
      return { data: null, error: `${res.status}: ${errText}` };
    }

    return { data: await res.json(), error: null };
  } catch (e) {
    return { data: null, error: e.message };
  }
}
