import {
  WORKER_CONCURRENCY,
  WORKER_POLL_INTERVAL_MS,
  WORKER_STALE_MINUTES,
} from "../config.js";
import { markCrashedStaleRuns, pickNextQueuedRun } from "../db/tasks.js";
import { executeRun } from "../research/run.js";

export type WorkerStats = {
  inflight: number;
  done: number;
  errored: number;
};

export async function startWorker(): Promise<{
  stop: () => Promise<void>;
  stats: () => WorkerStats;
}> {
  let stopping = false;
  const stats: WorkerStats = { inflight: 0, done: 0, errored: 0 };
  const inflight = new Set<Promise<void>>();

  const stale = await markCrashedStaleRuns(WORKER_STALE_MINUTES);
  if (stale > 0) console.log(`[worker] marked ${stale} stale running run(s) as failed_crashed`);

  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

  const loop = (async () => {
    console.log(
      `[worker] started: concurrency=${WORKER_CONCURRENCY}, poll=${WORKER_POLL_INTERVAL_MS}ms`,
    );
    while (!stopping) {
      if (inflight.size >= WORKER_CONCURRENCY) {
        await Promise.race(inflight);
        continue;
      }
      const next = await pickNextQueuedRun();
      if (next === null) {
        if (inflight.size === 0) await sleep(WORKER_POLL_INTERVAL_MS);
        else await Promise.race(inflight);
        continue;
      }

      console.log(
        `[worker] picked run=${next.runId} task=${next.taskId} article=${next.article}`,
      );
      stats.inflight = inflight.size + 1;
      const p = executeRun(next.runId, next.article)
        .then((res) => {
          stats.done += 1;
          console.log(
            `[worker] run=${res.runId} → ${res.status} storage=${res.storageDir}`,
          );
        })
        .catch((err: unknown) => {
          stats.errored += 1;
          console.error(
            `[worker] run=${next.runId} threw uncaught error:`,
            err instanceof Error ? err.message : err,
          );
        })
        .finally(() => {
          inflight.delete(p);
          stats.inflight = inflight.size;
        });
      inflight.add(p);
    }
    if (inflight.size > 0) {
      console.log(`[worker] draining ${inflight.size} inflight run(s)...`);
      await Promise.allSettled(inflight);
    }
    console.log("[worker] stopped");
  })();

  return {
    stop: async () => {
      stopping = true;
      await loop;
    },
    stats: () => ({ ...stats }),
  };
}
