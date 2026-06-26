/**
 * Streaming proxy for chatbot backend.
 *
 * Next.js rewrites() buffer SSE responses, breaking real-time streaming.
 * This Route Handler pipes the backend response body directly to the client
 * as a ReadableStream, so SSE events arrive as they're emitted.
 */

const CHATBOT_BACKEND =
  process.env.CHATBOT_BACKEND_URL || "http://localhost:8080";

export async function POST(request, { params }) {
  const { path } = await params;
  const backendUrl = `${CHATBOT_BACKEND}/${path.join("/")}`;

  const body = await request.text();

  const backendRes = await fetch(backendUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });

  // Pass the backend's readable stream straight through
  return new Response(backendRes.body, {
    status: backendRes.status,
    headers: {
      "Content-Type":
        backendRes.headers.get("Content-Type") || "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
}