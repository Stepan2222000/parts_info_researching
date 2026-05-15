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

export async function createQueuedRun(taskId: number): Promise<number> {
  const { rows } = await pool.query<{ id: number }>(
    "INSERT INTO task_runs (task_id, status) VALUES ($1, 'queued') RETURNING id",
    [taskId],
  );
  return rows[0]!.id;
}

// Атомарно достать одну queued-задачу и пометить running.
// Возвращает null, если очередь пуста.
export async function pickNextQueuedRun(): Promise<
  { runId: number; taskId: number; article: string } | null
> {
  const { rows } = await pool.query<{ run_id: number; task_id: number; article: string }>(
    `UPDATE task_runs r
       SET status = 'running', started_at = now()
       WHERE r.id = (
         SELECT id FROM task_runs
           WHERE status = 'queued'
           ORDER BY id
           FOR UPDATE SKIP LOCKED
           LIMIT 1
       )
       RETURNING r.id AS run_id, r.task_id, (SELECT article FROM tasks WHERE id = r.task_id) AS article`,
  );
  return rows[0] ? { runId: rows[0].run_id, taskId: rows[0].task_id, article: rows[0].article } : null;
}

export async function setRunStorage(runId: number, storageDir: string): Promise<void> {
  await pool.query("UPDATE task_runs SET storage_dir = $1 WHERE id = $2", [storageDir, runId]);
}

// Если run ещё queued (one-off CLI), переводим в running и заполняем started_at.
export async function ensureRunRunning(runId: number): Promise<void> {
  await pool.query(
    `UPDATE task_runs SET status = 'running', started_at = COALESCE(started_at, now())
       WHERE id = $1 AND status = 'queued'`,
    [runId],
  );
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

export async function getRunStorageDir(runId: number): Promise<string | null> {
  const { rows } = await pool.query<{ storage_dir: string | null }>(
    "SELECT storage_dir FROM task_runs WHERE id = $1",
    [runId],
  );
  return rows[0]?.storage_dir ?? null;
}

// При старте worker'а помечает зависшие running-задачи (>30 минут) как failed_crashed.
export async function markCrashedStaleRuns(timeoutMinutes = 30): Promise<number> {
  const { rowCount } = await pool.query(
    `UPDATE task_runs
       SET status = 'failed_crashed',
           error = 'worker died or stalled before finishing',
           finished_at = now()
       WHERE status = 'running'
         AND started_at < now() - ($1 || ' minutes')::interval`,
    [String(timeoutMinutes)],
  );
  return rowCount ?? 0;
}
