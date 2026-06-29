const BACKEND =
  process.env.OPENFINANCE_CHATBOT_BACKEND_URL || "http://localhost:8003";

export async function POST(request, { params }) {
  const { path } = await params;
  const backendUrl = `${BACKEND}/${path.join("/")}`;
  const body = await request.text();

  const backendRes = await fetch(backendUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });

  return new Response(backendRes.body, {
    status: backendRes.status,
    headers: {
      "Content-Type":
        backendRes.headers.get("Content-Type") || "application/json",
    },
  });
}
