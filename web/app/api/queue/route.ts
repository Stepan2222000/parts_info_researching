import { API_BASE } from "@/lib/server";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const limit = new URL(req.url).searchParams.get("limit") ?? "100";
  const r = await fetch(`${API_BASE}/queue?limit=${limit}`, { cache: "no-store" });
  return new Response(await r.text(), {
    status: r.status,
    headers: { "content-type": "application/json" },
  });
}
