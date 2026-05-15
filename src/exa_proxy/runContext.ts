import { AsyncLocalStorage } from "node:async_hooks";

export type ProxyCtx = {
  runId: number | null;
  curatorSessionId: number | null;
};

export const runCtxStorage = new AsyncLocalStorage<ProxyCtx>();

export function getRunId(): number | null {
  return runCtxStorage.getStore()?.runId ?? null;
}

export function getCuratorSessionId(): number | null {
  return runCtxStorage.getStore()?.curatorSessionId ?? null;
}
