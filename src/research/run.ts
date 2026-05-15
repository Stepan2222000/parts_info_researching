import { mkdir, readFile, writeFile } from "node:fs/promises";
import { relative, resolve } from "node:path";
import { CODEX_RULES_PATH, STORAGE_ROOT, PROJECT_ROOT } from "../config.js";
import { saveDraftResult } from "../db/drafts.js";
import { createRun, createTask, finishRun, setRunStorage, setRunThreadId } from "../db/tasks.js";
import { EXA_NUM_RESULTS, callExaSearch, exaResultContainsArticle } from "./exa.js";
import { createResearchThread, runTurnStreamed } from "./codex.js";
import {
  buildCodexPrompt,
  buildExaQuery,
  buildKitContentsPrompt,
  buildKitContentsQuery,
  buildLowConfidencePrompt,
  buildLowConfidenceQuery,
} from "./prompts.js";
import {
  getConfirmedArticles,
  getLowConfidenceArticles,
  validateStructuredResult,
  type StructuredResult,
} from "./validation.js";

const ARTICLE_RE = /^[A-Z0-9\-]+$/;

export class NoExactDataError extends Error {}
export class ValidationError extends Error {}

export type RunResult = {
  taskId: number;
  runId: number;
  status: "done" | "failed_no_data" | "failed_validation";
  storageDir: string;
};

export async function runResearchTask(rawArticle: string): Promise<RunResult> {
  const article = rawArticle.trim().toUpperCase();
  if (!ARTICLE_RE.test(article)) {
    throw new Error(`Article must match ${ARTICLE_RE}, got "${rawArticle}"`);
  }

  const taskId = await createTask(article);
  const runId = await createRun(taskId);

  const storageDirAbs = resolve(STORAGE_ROOT, "runs", String(runId));
  const storageDirRel = relative(PROJECT_ROOT, storageDirAbs);
  await mkdir(storageDirAbs, { recursive: true });
  await setRunStorage(runId, storageDirRel);

  const codexRules = await readFile(CODEX_RULES_PATH, "utf8");

  const exaMainPath = resolve(storageDirAbs, "exa_main.json");
  const exaLowConfPath = resolve(storageDirAbs, "exa_low_confidence.json");
  const exaKitPath = resolve(storageDirAbs, "exa_kit_contents.json");
  const codexResultPath = resolve(storageDirAbs, "codex_result.json");
  const messagesJsonlPath = resolve(storageDirAbs, "research_messages.jsonl");

  try {
    // 1. Main Exa search.
    const mainQuery = buildExaQuery(article);
    const mainExa = await callExaSearch(mainQuery);
    await writeFile(
      exaMainPath,
      JSON.stringify(
        {
          task_part_number: article,
          tool: "web_search_exa",
          num_results: EXA_NUM_RESULTS,
          query: mainQuery,
          raw_exa_result: mainExa,
        },
        null,
        2,
      ),
    );

    if (!exaResultContainsArticle(mainExa, article)) {
      throw new NoExactDataError(
        `Exa result does not contain exact part number "${article}"`,
      );
    }

    // 2. Codex first turn — основной структурный JSON.
    const thread = createResearchThread(storageDirAbs);
    await runTurnStreamed(
      thread,
      buildCodexPrompt({
        partNumber: article,
        exaJsonPath: exaMainPath,
        outputJsonPath: codexResultPath,
        codexRules,
      }),
      messagesJsonlPath,
    );
    if (thread.id) await setRunThreadId(runId, thread.id);

    let result = await readAndValidate(codexResultPath, article);

    // 3. Low-confidence pass.
    const lowConf = getLowConfidenceArticles(result);
    if (lowConf.length > 0) {
      const q = buildLowConfidenceQuery(article, lowConf);
      const exa = await callExaSearch(q);
      await writeFile(
        exaLowConfPath,
        JSON.stringify(
          {
            task_part_number: article,
            tool: "web_search_exa",
            num_results: EXA_NUM_RESULTS,
            query: q,
            checked_articles: lowConf,
            raw_exa_result: exa,
          },
          null,
          2,
        ),
      );
      await runTurnStreamed(
        thread,
        buildLowConfidencePrompt({
          partNumber: article,
          outputJsonPath: codexResultPath,
          lowConfidenceExaJsonPath: exaLowConfPath,
          articles: lowConf,
          codexRules,
        }),
        messagesJsonlPath,
      );
      result = await readAndValidate(codexResultPath, article);
    }

    // 4. Kit contents pass.
    if (result.is_kit) {
      const confirmed = getConfirmedArticles(result);
      const q = buildKitContentsQuery(article, confirmed);
      const exa = await callExaSearch(q);
      await writeFile(
        exaKitPath,
        JSON.stringify(
          {
            task_part_number: article,
            tool: "web_search_exa",
            num_results: EXA_NUM_RESULTS,
            query: q,
            raw_exa_result: exa,
          },
          null,
          2,
        ),
      );
      if (!exaResultContainsArticle(exa, article)) {
        throw new NoExactDataError(
          `Exa kit-contents result does not contain exact part number "${article}"`,
        );
      }
      await runTurnStreamed(
        thread,
        buildKitContentsPrompt({
          partNumber: article,
          outputJsonPath: codexResultPath,
          kitContentsExaJsonPath: exaKitPath,
          codexRules,
        }),
        messagesJsonlPath,
      );
      result = await readAndValidate(codexResultPath, article);
    }

    // 5. Parse into draft tables.
    await saveDraftResult(runId, result);
    await finishRun(runId, "done");
    return { taskId, runId, status: "done", storageDir: storageDirRel };
  } catch (err) {
    if (err instanceof NoExactDataError) {
      await finishRun(runId, "failed_no_data", err.message);
      return { taskId, runId, status: "failed_no_data", storageDir: storageDirRel };
    }
    if (err instanceof ValidationError) {
      await finishRun(runId, "failed_validation", err.message);
      return { taskId, runId, status: "failed_validation", storageDir: storageDirRel };
    }
    await finishRun(runId, "failed_crashed", err instanceof Error ? err.message : String(err));
    throw err;
  }
}

async function readAndValidate(path: string, article: string): Promise<StructuredResult> {
  let parsed: unknown;
  try {
    const text = await readFile(path, "utf8");
    parsed = JSON.parse(text);
  } catch (err) {
    throw new ValidationError(
      `Failed to read/parse codex result: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
  try {
    validateStructuredResult(parsed, article);
  } catch (err) {
    throw new ValidationError(err instanceof Error ? err.message : String(err));
  }
  return parsed as StructuredResult;
}
