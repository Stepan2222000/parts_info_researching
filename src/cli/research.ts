import { pool } from "../db/pool.js";
import { runResearchTask } from "../research/run.js";

const article = process.argv[2];
if (!article) {
  console.error("Usage: tsx src/cli/research.ts <ARTICLE>");
  process.exit(1);
}

try {
  const r = await runResearchTask(article);
  console.log(`task=${r.taskId} run=${r.runId} status=${r.status} storage=${r.storageDir}`);
  process.exit(r.status === "done" ? 0 : 2);
} finally {
  await pool.end();
}
