import "dotenv/config";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
export const PROJECT_ROOT = resolve(here, "..");

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required env var: ${name}`);
  }
  return value;
}

export const PARTS_RESEARCH_DATABASE_URL = required("PARTS_RESEARCH_DATABASE_URL");
export const EXA_API_KEY = required("EXA_API_KEY");
export const STORAGE_ROOT = process.env.STORAGE_ROOT
  ? resolve(process.env.STORAGE_ROOT)
  : resolve(PROJECT_ROOT, "storage");
export const CODEX_RULES_PATH = resolve(PROJECT_ROOT, "codex_rules.md");
export const EXA_PROXY_URL = process.env.EXA_PROXY_URL ?? "http://127.0.0.1:8765/";
export const WORKER_CONCURRENCY = parseInt(process.env.WORKER_CONCURRENCY ?? "30", 10);
export const WORKER_POLL_INTERVAL_MS = parseInt(process.env.WORKER_POLL_INTERVAL_MS ?? "2000", 10);
export const WORKER_STALE_MINUTES = parseInt(process.env.WORKER_STALE_MINUTES ?? "30", 10);
export const CURATOR_MODEL = process.env.CURATOR_MODEL ?? "gpt-5.5";
export const CURATOR_REASONING_EFFORT =
  (process.env.CURATOR_REASONING_EFFORT as
    | "minimal"
    | "low"
    | "medium"
    | "high"
    | "xhigh"
    | undefined) ?? "high";
export const CURATOR_RULES_PATH = resolve(PROJECT_ROOT, "curator_rules.md");
