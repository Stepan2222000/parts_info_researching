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
