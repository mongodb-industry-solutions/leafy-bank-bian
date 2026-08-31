const CORE_BASE = "/api/backend";

// --- the PartyAuthentication session token (BIAN SD 38917) ------------------
//
// Set once by UserContext when a persona authenticates, then attached automatically to
// the BIAN services' calls. It replaces the arrangement where `customerId` was a body
// field the browser chose freely: the transactions service now derives the caller's
// identity from this signature instead.
//
// Module scope, not React state, so every caller gets it without threading a token
// through a dozen hooks. It dies on a full page load, which is the right lifetime —
// UserContext re-issues on hydrate.
let sessionToken = null;

// The assessment id (`SESS-…`) the current token belongs to. BIAN addresses a step-up as a
// sub-resource of its assessment — `/PartyAuthentication/{id}/Question/Evaluate` — so the
// channel has to know which session it is strengthening. Kept beside the token rather than
// decoded from it on demand: the two always change together, and a client that parses a JWT
// it cannot verify invites treating other claims as trustworthy.
let sessionRef = null;

export function setSessionToken(token, ref = null) {
  sessionToken = token || null;
  sessionRef = token ? ref : null;
}

export function currentSessionRef() {
  return sessionRef;
}

// Attached ONLY to the BIAN services. Mirrors `BACKEND_BY_PREFIX` in
// app/api/backend/[...path]/route.js — deliberately a list and not "every request",
// because the fallback backend is the open-finance monolith whose `/secure/*` routes
// expect their OWN bearer token. Blanket-attaching here would send the wrong credential
// to routes that check one, which is how the 2026-07-06 proxy regression happened: a
// convention needed by one backend applied globally.
const AUTHENTICATED_PREFIXES = new Set([
  "PartyAuthentication",
  "PartyReferenceDataDirectory",
  "CurrentAccount",
  "PaymentOrderInitiation",
  "workflow",
  "pipeline",
]);

function sessionTokenFor(path) {
  const prefix = String(path || "").split("/")[0];
  return AUTHENTICATED_PREFIXES.has(prefix) ? sessionToken : null;
}
const CHATBOT_BASE = "/api/chatbot";
const OPENFINANCE_CHAT_BASE = "/api/openfinance-chat";

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
  // An explicit token wins: the open-finance consent flow passes its own, and that is a
  // different credential for a different backend.
  const token = bearerToken || sessionTokenFor(path);
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
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
 * Back-office payments workflow client (transactions service via the proxy).
 *
 * Mirrors pipelineApi. The two are deliberately separate clients rather than one
 * parameterised helper: they hit different services, and the UI composing both halves of
 * a payment's trace should read as two reads, not one.
 *
 * @param {string} path - path after the prefix, e.g. "payments" or "payments/PAY-123"
 * @param {object} [params] - query params as key-value pairs
 * @returns {Promise<{data: any, error: string|null}>}
 */
export async function workflowApi(path, params = null) {
  let url = `${CORE_BASE}/workflow/${path}`;
  if (params) {
    const clean = Object.fromEntries(
      Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== "")
    );
    const qs = new URLSearchParams(clean).toString();
    if (qs) url += (url.includes("?") ? "&" : "?") + qs;
  }

  try {
    const res = await fetch(url, {
      method: "GET",
      headers: { "Content-Type": "application/json" },
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
