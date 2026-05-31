import { API_BASE } from "@/lib/server";

export const dynamic = "force-dynamic";

export async function GET() {
  const r = await fetch(`${API_BASE}/curator/sessions`, { cache: "no-store" });
  return new Response(await r.text(), {
    status: r.status,
    headers: { "content-type": "application/json" },
  });
}

export async function POST() {
  const r = await fetch(`${API_BASE}/curator/sessions`, { method: "POST" });
  return new Response(await r.text(), {
    status: r.status,
    headers: { "content-type": "application/json" },
  });
}
