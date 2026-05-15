import { pool } from "../../db/pool.js";

export type MarkOutcome = { ok: true } | { ok: false; error: string };

export async function markNeedsReview(runId: number, reason: string): Promise<MarkOutcome> {
  const { rowCount } = await pool.query(
    `UPDATE task_runs
       SET status = 'needs_human_review', error = $1, finished_at = COALESCE(finished_at, now())
       WHERE id = $2`,
    [reason, runId],
  );
  if ((rowCount ?? 0) === 0) return { ok: false, error: `run ${runId} not found` };
  return { ok: true };
}
