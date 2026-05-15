import { pool } from "./pool.js";

export type TaskRunStatus =
  | "queued"
  | "running"
  | "done"
  | "failed_no_data"
  | "failed_validation"
  | "failed_crashed"
  | "needs_human_review";

export async function createTask(article: string): Promise<number> {
  const { rows } = await pool.query<{ id: number }>(
    "INSERT INTO tasks (article) VALUES ($1) RETURNING id",
    [article],
  );
  return rows[0]!.id;
}

export async function createRun(taskId: number): Promise<number> {
  const { rows } = await pool.query<{ id: number }>(
    "INSERT INTO task_runs (task_id, status) VALUES ($1, 'running') RETURNING id",
    [taskId],
  );
  return rows[0]!.id;
}

export async function setRunStorage(runId: number, storageDir: string): Promise<void> {
  await pool.query("UPDATE task_runs SET storage_dir = $1 WHERE id = $2", [storageDir, runId]);
}

export async function setRunThreadId(runId: number, threadId: string): Promise<void> {
  await pool.query("UPDATE task_runs SET codex_thread_id = $1 WHERE id = $2", [threadId, runId]);
}

export async function finishRun(
  runId: number,
  status: Exclude<TaskRunStatus, "queued" | "running">,
  error?: string,
): Promise<void> {
  await pool.query(
    "UPDATE task_runs SET status = $1, error = $2, finished_at = now() WHERE id = $3",
    [status, error ?? null, runId],
  );
}
