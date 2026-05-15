import { pool } from "../db/pool.js";
import { startWorker } from "../queue/worker.js";

const { stop } = await startWorker();

const shutdown = async (sig: string) => {
  console.log(`[worker-cli] ${sig} received, stopping`);
  await stop();
  await pool.end();
  process.exit(0);
};
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
