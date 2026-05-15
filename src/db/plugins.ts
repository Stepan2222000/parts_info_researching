import { pool } from "./pool.js";

export async function savePluginPayload(
  runId: number,
  pluginName: string,
  payload: unknown,
): Promise<void> {
  await pool.query(
    "INSERT INTO plugin_payloads (run_id, plugin_name, payload) VALUES ($1, $2, $3)",
    [runId, pluginName, payload],
  );
}
