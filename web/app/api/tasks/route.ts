import { API_BASE } from "@/lib/server";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const body = await req.text();
  const r = await fetch(`${API_BASE}/tasks`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });
  return new Response(await r.text(), {
    status: r.status,
    headers: { "content-type": "application/json" },
  });
}
