import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { PROJECT_ROOT } from "../config.js";
import { getRunStorageDir } from "../db/tasks.js";
import { loadAllowedBrands, loadAllowedProductTypes } from "../db/context.js";
import { validateStructuredResult } from "../research/validation.js";

export type WriteResultOutcome =
  | { ok: true; absPath: string }
  | { ok: false; error: string };

export async function writeResultForRun(
  runId: number,
  partNumber: string,
  jsonPayload: unknown,
): Promise<WriteResultOutcome> {
  const storageDirRel = await getRunStorageDir(runId);
  if (!storageDirRel) {
    return { ok: false, error: `run ${runId} has no storage_dir` };
  }

  let allowedBrands: string[];
  let allowedProductTypes: string[];
  try {
    [allowedBrands, allowedProductTypes] = await Promise.all([
      loadAllowedBrands(),
      loadAllowedProductTypes(),
    ]);
  } catch (e) {
    return { ok: false, error: `failed to load Smart catalogs: ${msg(e)}` };
  }

  try {
    validateStructuredResult(jsonPayload, {
      expectedPartNumber: partNumber,
      allowedBrands,
      allowedProductTypes,
    });
  } catch (e) {
    return { ok: false, error: `validation failed: ${msg(e)}` };
  }

  const absPath = resolve(PROJECT_ROOT, storageDirRel, "codex_result.json");
  await mkdir(dirname(absPath), { recursive: true });
  await writeFile(absPath, JSON.stringify(jsonPayload, null, 2));
  return { ok: true, absPath };
}

function msg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
