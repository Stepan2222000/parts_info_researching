import { lookupCache, recordUsage, storeCache } from "../db/cache.js";
import { callExaTool } from "./exaClient.js";

// Унифицированный exact-match кэш + запись usage. Используется и сервером
// (когда вызов идёт от Codex-агента), и оркестратором (когда обязательные
// поиски делает сам backend).
export async function cachedExaCall(
  toolName: string,
  args: Record<string, unknown>,
  runId: number | null,
): Promise<unknown> {
  const cached = await lookupCache(toolName, args);
  if (cached.hit) {
    if (runId !== null) await recordUsage(cached.cacheId, runId, true);
    return cached.response;
  }
  const response = await callExaTool(toolName, args);
  const cacheId = await storeCache(toolName, args, response);
  if (runId !== null) await recordUsage(cacheId, runId, false);
  return response;
}
