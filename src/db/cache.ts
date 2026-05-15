import { createHash } from "node:crypto";
import { pool } from "./pool.js";

export type CacheLookup =
  | { hit: true; cacheId: number; response: unknown }
  | { hit: false };

export function computeRequestHash(toolName: string, args: unknown): string {
  const canonical = canonicalize(args);
  return createHash("sha256").update(toolName).update("\0").update(canonical).digest("hex");
}

function canonicalize(v: unknown): string {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(canonicalize).join(",") + "]";
  const keys = Object.keys(v as Record<string, unknown>).sort();
  return (
    "{" +
    keys
      .map((k) => JSON.stringify(k) + ":" + canonicalize((v as Record<string, unknown>)[k]))
      .join(",") +
    "}"
  );
}

export async function lookupCache(toolName: string, args: unknown): Promise<CacheLookup> {
  const hash = computeRequestHash(toolName, args);
  const { rows } = await pool.query<{ id: number; response: unknown }>(
    `UPDATE exa_cache
       SET hit_count = hit_count + 1, last_used_at = now()
       WHERE request_hash = $1
       RETURNING id, response`,
    [hash],
  );
  if (rows.length === 0) return { hit: false };
  return { hit: true, cacheId: rows[0]!.id, response: rows[0]!.response };
}

export async function storeCache(
  toolName: string,
  args: unknown,
  response: unknown,
): Promise<number> {
  const hash = computeRequestHash(toolName, args);
  const { rows } = await pool.query<{ id: number }>(
    `INSERT INTO exa_cache (request_hash, tool_name, arguments, response)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (request_hash) DO UPDATE SET last_used_at = now()
       RETURNING id`,
    [hash, toolName, args, response],
  );
  return rows[0]!.id;
}

export async function recordUsage(cacheId: number, runId: number, hit: boolean): Promise<void> {
  await pool.query(
    "INSERT INTO exa_cache_usage (cache_id, run_id, hit) VALUES ($1, $2, $3)",
    [cacheId, runId, hit],
  );
}
