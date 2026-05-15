import { pool } from "../db/pool.js";

export type QueueSnapshot = {
  done_unpublished: number;
  done_total: number;
  needs_human_review: number;
  queued: number;
  running: number;
  failed_no_data: number;
  failed_validation: number;
  failed_crashed: number;
};

export async function loadSnapshot(): Promise<QueueSnapshot> {
  const { rows } = await pool.query<{
    done_unpublished: string;
    done_total: string;
    needs_human_review: string;
    queued: string;
    running: string;
    failed_no_data: string;
    failed_validation: string;
    failed_crashed: string;
  }>(`
    SELECT
      COUNT(*) FILTER (WHERE r.status = 'done'
                        AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.run_id = r.id))::text AS done_unpublished,
      COUNT(*) FILTER (WHERE r.status = 'done')::text                AS done_total,
      COUNT(*) FILTER (WHERE r.status = 'needs_human_review')::text  AS needs_human_review,
      COUNT(*) FILTER (WHERE r.status = 'queued')::text              AS queued,
      COUNT(*) FILTER (WHERE r.status = 'running')::text             AS running,
      COUNT(*) FILTER (WHERE r.status = 'failed_no_data')::text      AS failed_no_data,
      COUNT(*) FILTER (WHERE r.status = 'failed_validation')::text   AS failed_validation,
      COUNT(*) FILTER (WHERE r.status = 'failed_crashed')::text      AS failed_crashed
    FROM task_runs r
  `);
  const r = rows[0]!;
  return {
    done_unpublished: parseInt(r.done_unpublished, 10),
    done_total: parseInt(r.done_total, 10),
    needs_human_review: parseInt(r.needs_human_review, 10),
    queued: parseInt(r.queued, 10),
    running: parseInt(r.running, 10),
    failed_no_data: parseInt(r.failed_no_data, 10),
    failed_validation: parseInt(r.failed_validation, 10),
    failed_crashed: parseInt(r.failed_crashed, 10),
  };
}

export function formatSnapshot(s: QueueSnapshot): string {
  return [
    "<queue>",
    `done без публикации: ${s.done_unpublished}`,
    `done всего: ${s.done_total}`,
    `needs_human_review: ${s.needs_human_review}`,
    `queued: ${s.queued}`,
    `running: ${s.running}`,
    `failed_no_data: ${s.failed_no_data}`,
    `failed_validation: ${s.failed_validation}`,
    `failed_crashed: ${s.failed_crashed}`,
    "</queue>",
  ].join("\n");
}
