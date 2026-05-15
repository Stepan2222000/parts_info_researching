import { pool } from "./pool.js";

export type CuratorRole = "user" | "assistant" | "tool";

export async function createSession(workingDir: string): Promise<number> {
  const { rows } = await pool.query<{ id: number }>(
    "INSERT INTO curator_sessions (working_dir) VALUES ($1) RETURNING id",
    [workingDir],
  );
  return rows[0]!.id;
}

export async function setSessionThreadId(sessionId: number, threadId: string): Promise<void> {
  await pool.query(
    "UPDATE curator_sessions SET codex_thread_id = $1 WHERE id = $2",
    [threadId, sessionId],
  );
}

export async function endSession(sessionId: number): Promise<void> {
  await pool.query(
    "UPDATE curator_sessions SET ended_at = now() WHERE id = $1 AND ended_at IS NULL",
    [sessionId],
  );
}

export async function insertMessage(
  sessionId: number,
  role: CuratorRole,
  content: string | null,
  toolCall: unknown,
): Promise<void> {
  await pool.query(
    "INSERT INTO curator_messages (session_id, role, content, tool_call) VALUES ($1, $2, $3, $4)",
    [sessionId, role, content, toolCall ?? null],
  );
}

export async function countMessages(sessionId: number): Promise<number> {
  const { rows } = await pool.query<{ c: string }>(
    "SELECT COUNT(*)::text AS c FROM curator_messages WHERE session_id = $1",
    [sessionId],
  );
  return parseInt(rows[0]!.c, 10);
}

export async function startSqlLog(sessionId: number, sqlText: string): Promise<number> {
  const { rows } = await pool.query<{ id: number }>(
    "INSERT INTO agent_sql_log (session_id, sql_text) VALUES ($1, $2) RETURNING id",
    [sessionId, sqlText],
  );
  return rows[0]!.id;
}

export async function finishSqlLog(
  logId: number,
  rowsAffected: number | null,
  error: string | null,
): Promise<void> {
  await pool.query(
    "UPDATE agent_sql_log SET rows_affected = $1, error = $2, finished_at = now() WHERE id = $3",
    [rowsAffected, error, logId],
  );
}

export async function recordPublication(
  client: { query: typeof pool.query },
  runId: number,
  curatorSessionId: number,
  smartTable: string,
  smartId: string,
  action: "insert" | "update",
): Promise<void> {
  await client.query(
    `INSERT INTO publications (run_id, curator_session_id, smart_table, smart_id, action)
       VALUES ($1, $2, $3, $4, $5)`,
    [runId, curatorSessionId, smartTable, smartId, action],
  );
}
