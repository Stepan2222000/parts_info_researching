import { pool } from "../db/pool.js";
import { enqueueResearch } from "../research/run.js";

const articles = process.argv.slice(2);
if (articles.length === 0) {
  console.error("Usage: tsx src/cli/submit.ts ARTICLE [ARTICLE ...]");
  process.exit(1);
}

try {
  for (const a of articles) {
    const { taskId, runId, article } = await enqueueResearch(a);
    console.log(`queued task=${taskId} run=${runId} article=${article}`);
  }
} finally {
  await pool.end();
}
