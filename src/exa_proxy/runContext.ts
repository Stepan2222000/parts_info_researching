import { AsyncLocalStorage } from "node:async_hooks";

export type RunCtx = { runId: number | null };
export const runCtxStorage = new AsyncLocalStorage<RunCtx>();

export function getRunId(): number | null {
  return runCtxStorage.getStore()?.runId ?? null;
}
