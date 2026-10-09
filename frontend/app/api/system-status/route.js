/**
 * Probes each backend's /health from the server, so the browser needs no direct access
 * to the services. Used by the Operations dashboard's status pill.
 */
export const dynamic = "force-dynamic";

const SERVICES = [
  ["Accounts", process.env.ACCOUNTS_BACKEND_URL || "http://localhost:8001"],
  ["Transactions", process.env.TRANSACTIONS_BACKEND_URL || "http://localhost:8002"],
  ["Ledger", process.env.LEDGER_BACKEND_URL || "http://localhost:8080"],
  ["Agents", process.env.AGENTS_BACKEND_URL || "http://localhost:8004"],
];

async function probe([name, base]) {
  try {
    const res = await fetch(`${base}/health`, { signal: AbortSignal.timeout(2000), cache: "no-store" });
    return { name, ok: res.ok };
  } catch {
    return { name, ok: false };
  }
}

export async function GET() {
  const services = await Promise.all(SERVICES.map(probe));
  return Response.json({ operational: services.every((s) => s.ok), services });
}
