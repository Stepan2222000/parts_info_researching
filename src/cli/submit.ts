import { pool } from "../db/pool.js";
import { AlreadyFinalizedError, enqueueResearch } from "../research/run.js";

const articles = process.argv.slice(2);
if (articles.length === 0) {
  console.error("Usage: tsx src/cli/submit.ts ARTICLE [ARTICLE ...]");
  process.exit(1);
}

let hadError = false;
try {
  for (const a of articles) {
    try {
      const { taskId, runId, article } = await enqueueResearch(a);
      console.log(`queued task=${taskId} run=${runId} article=${article}`);
    } catch (err) {
      hadError = true;
      if (err instanceof AlreadyFinalizedError) {
        console.error(`skipped ${a}: ${err.message}`);
      } else {
        console.error(`failed ${a}: ${err instanceof Error ? err.message : String(err)}`);
      }
    }
  }
} finally {
  await pool.end();
}
process.exit(hadError ? 2 : 0);
