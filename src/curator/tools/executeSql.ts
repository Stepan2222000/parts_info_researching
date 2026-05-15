import { pool } from "../../db/pool.js";
import { finishSqlLog, startSqlLog } from "../../db/curator.js";

export type ExecuteSqlOutcome =
  | { ok: true; rows: unknown[]; row_count: number; command: string }
  | { ok: false; error: string };

export async function executeSqlForCurator(
  sessionId: number,
  sql: string,
): Promise<ExecuteSqlOutcome> {
  const logId = await startSqlLog(sessionId, sql);
  try {
    const res = await pool.query(sql);
    const single = Array.isArray(res) ? res[res.length - 1]! : res;
    const command = (single as { command?: string }).command ?? "";
    const rows = (single as { rows?: unknown[] }).rows ?? [];
    const rowCount = (single as { rowCount?: number }).rowCount ?? rows.length;
    await finishSqlLog(logId, rowCount, null);
    return { ok: true, rows, row_count: rowCount, command };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    await finishSqlLog(logId, null, msg);
    return { ok: false, error: msg };
  }
}
