import { pool } from "../db/pool.js";
import { runRepl } from "../curator/repl.js";

try {
  await runRepl();
} finally {
  await pool.end();
}
