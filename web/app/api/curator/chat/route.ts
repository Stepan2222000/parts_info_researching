import { API_BASE } from "@/lib/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// Thin SSE passthrough: useChat → this route → FastAPI /curator/message.
// The Python side already emits the AI SDK v6 UI Message Stream, so we just
// forward the byte stream and preserve the protocol header.
export async function POST(req: Request) {
  const body = await req.text();
  const upstream = await fetch(`${API_BASE}/curator/message`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });

  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "x-vercel-ai-ui-message-stream": "v1",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
    },
  });
}
