/**
 * Runtime proxy for the core backend API.
 *
 * Replaces the next.config.mjs rewrite which bakes the backend URL at build
 * time. This Route Handler reads the backend URLs at runtime so the deployed
 * container picks them up from the Helm chart env.
 *
 * The BIAN backend is split across services. We route by the first path
 * segment (the BIAN service-domain name):
 *   - CurrentAccountFulfillmentArrangement/*, PartyReferenceDataDirectoryEntry/*
 *       → ACCOUNTS_BACKEND_URL     (accounts service)
 *   - PaymentOrderProcedure/*       → TRANSACTIONS_BACKEND_URL (transactions service)
 *   - everything else (openfinance/*, encryption-demo/*, …)
 *       → CORE_BACKEND_URL          (open-finance monolith — fallback)
 */

const CORE_BACKEND =
  process.env.CORE_BACKEND_URL || "http://localhost:8001";
const ACCOUNTS_BACKEND =
  process.env.ACCOUNTS_BACKEND_URL || "http://localhost:8001";
const TRANSACTIONS_BACKEND =
  process.env.TRANSACTIONS_BACKEND_URL || "http://localhost:8002";

// First path segment (BIAN service domain) → backend base URL.
// Unmapped prefixes fall through to CORE_BACKEND.
const BACKEND_BY_PREFIX = {
  PartyReferenceDataDirectoryEntry: ACCOUNTS_BACKEND,
  CurrentAccountFulfillmentArrangement: ACCOUNTS_BACKEND,
  PaymentOrderProcedure: TRANSACTIONS_BACKEND,
};

async function proxy(request, { params }) {
  const { path } = await params;
  let backendPath = `/${path.join("/")}`;

  // Preserve trailing slash for FastAPI (avoids 307 redirects)
  if (request.nextUrl.pathname.endsWith("/")) {
    backendPath += "/";
  }

  const backend = BACKEND_BY_PREFIX[path[0]] || CORE_BACKEND;
  const backendUrl = `${backend}${backendPath}${request.nextUrl.search}`;

  const headers = new Headers();
  headers.set("Content-Type", request.headers.get("Content-Type") || "application/json");

  // Forward Authorization header if present
  const auth = request.headers.get("Authorization");
  if (auth) {
    headers.set("Authorization", auth);
  }

  const fetchOptions = {
    method: request.method,
    headers,
  };

  // Forward body for non-GET requests
  if (request.method !== "GET" && request.method !== "HEAD") {
    fetchOptions.body = await request.text();
  }

  const backendRes = await fetch(backendUrl, fetchOptions);

  return new Response(backendRes.body, {
    status: backendRes.status,
    headers: {
      "Content-Type": backendRes.headers.get("Content-Type") || "application/json",
    },
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
