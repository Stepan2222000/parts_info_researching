import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { relative, resolve } from "node:path";
import { CODEX_RULES_PATH, EXA_PROXY_URL, PROJECT_ROOT, STORAGE_ROOT } from "../config.js";
import { pool } from "../db/pool.js";
import { saveDraftResult } from "../db/drafts.js";
import {
  loadAllowedBrands,
  loadAllowedProductTypes,
  loadBrandAliases,
} from "../db/context.js";
import { savePluginPayload } from "../db/plugins.js";
import {
  createQueuedRun,
  createTask,
  ensureRunRunning,
  finishRun,
  setRunStorage,
  setRunThreadId,
  type TaskRunStatus,
} from "../db/tasks.js";
import { cachedExaCall } from "../exa_proxy/exaCached.js";
import { fetchSmartContext, formatSmartContextForPrompt } from "../plugins/smart.js";
import {
  buildExaQuery,
  buildKitContentsPrompt,
  buildKitContentsQuery,
  buildLowConfidencePrompt,
  buildLowConfidenceQuery,
  buildMainPrompt,
  type PromptContext,
} from "./prompts.js";
import { createResearchThread, runTurnStreamed } from "./codex.js";
import { EXA_NUM_RESULTS, exaResultContainsArticle } from "./exa.js";
import {
  getConfirmedArticles,
  getLowConfidenceArticles,
  validateStructuredResult,
  type StructuredResult,
  type ValidationContext,
} from "./validation.js";

const ARTICLE_RE = /^[A-Z0-9\-]+$/;

export class NoExactDataError extends Error {}
export class ValidationError extends Error {}
export class AlreadyFinalizedError extends Error {}

export type RunResult = {
  taskId: number;
  runId: number;
  status: TaskRunStatus;
  storageDir: string;
};

// Из CLI/submit: создать task + queued-run без выполнения.
// До создания task проверяем через FDW, что артикул не финализирован в Smart.
export async function enqueueResearch(rawArticle: string): Promise<{ taskId: number; runId: number; article: string }> {
  const article = normalizeArticle(rawArticle);
  const { rowCount } = await pool.query(
    "SELECT 1 FROM smart.parts WHERE $1 = ANY(articles) AND is_draft = false LIMIT 1",
    [article],
  );
  if ((rowCount ?? 0) > 0) {
    throw new AlreadyFinalizedError(
      `article ${article} is already finalized in Smart (is_draft=false), research skipped`,
    );
  }
  const taskId = await createTask(article);
  const runId = await createQueuedRun(taskId);
  return { taskId, runId, article };
}

function normalizeArticle(raw: string): string {
  const article = raw.trim().toUpperCase();
  if (!ARTICLE_RE.test(article)) {
    throw new Error(`Article must match ${ARTICLE_RE}, got "${raw}"`);
  }
  return article;
}

// Из worker'а или из одноразового CLI: выполнить уже существующий run.
export async function executeRun(runId: number, article: string): Promise<RunResult> {
  await ensureRunRunning(runId);

  const storageDirAbs = resolve(STORAGE_ROOT, "runs", String(runId));
  const storageDirRel = relative(PROJECT_ROOT, storageDirAbs);
  await mkdir(storageDirAbs, { recursive: true });
  await setRunStorage(runId, storageDirRel);

  const exaMainPath = resolve(storageDirAbs, "exa_main.json");
  const exaLowConfPath = resolve(storageDirAbs, "exa_low_confidence.json");
  const exaKitPath = resolve(storageDirAbs, "exa_kit_contents.json");
  const codexResultPath = resolve(storageDirAbs, "codex_result.json");
  const messagesJsonlPath = resolve(storageDirAbs, "research_messages.jsonl");

  try {
    const codexRules = await readFile(CODEX_RULES_PATH, "utf8");
    const [allowedBrands, allowedProductTypes, brandAliases, smartPayload] = await Promise.all([
      loadAllowedBrands(),
      loadAllowedProductTypes(),
      loadBrandAliases(),
      fetchSmartContext(article),
    ]);
    await savePluginPayload(runId, "smart", smartPayload);

    const promptCtx: PromptContext = {
      allowedBrands,
      allowedProductTypes,
      brandAliases,
      smartContextMarkdown: formatSmartContextForPrompt(smartPayload),
      codexRules,
    };
    const valCtx: ValidationContext = {
      expectedPartNumber: article,
      allowedBrands,
      allowedProductTypes,
    };

    // 1. Основной Exa-поиск (через кэш).
    const mainQuery = buildExaQuery(article);
    const mainExa = await cachedExaCall(
      "web_search_exa",
      { query: mainQuery, numResults: EXA_NUM_RESULTS },
      runId,
    );
    await writeExaFile(exaMainPath, article, mainQuery, mainExa);
    if (!exaResultContainsArticle(mainExa, article)) {
      throw new NoExactDataError(`Exa main search did not contain exact article "${article}".`);
    }

    // 2. Запускаем Codex thread с подключённым прокси.
    const thread = createResearchThread({
      workingDirectory: storageDirAbs,
      runId,
      exaProxyUrl: EXA_PROXY_URL,
    });

    await runTurnStreamed(
      thread,
      buildMainPrompt({
        partNumber: article,
        exaJsonPath: exaMainPath,
        outputJsonPath: codexResultPath,
        ctx: promptCtx,
      }),
      messagesJsonlPath,
    );
    if (thread.id) await setRunThreadId(runId, thread.id);

    let result = await readAndValidateResult(codexResultPath, valCtx);

    // 3. Low-confidence pass.
    const lowConf = getLowConfidenceArticles(result);
    if (lowConf.length > 0) {
      const q = buildLowConfidenceQuery(article, lowConf);
      const exa = await cachedExaCall(
        "web_search_exa",
        { query: q, numResults: EXA_NUM_RESULTS },
        runId,
      );
      await writeExaFile(exaLowConfPath, article, q, exa, { checked_articles: lowConf });
      await runTurnStreamed(
        thread,
        buildLowConfidencePrompt({
          partNumber: article,
          outputJsonPath: codexResultPath,
          lowConfidenceExaJsonPath: exaLowConfPath,
          articles: lowConf,
          ctx: promptCtx,
        }),
        messagesJsonlPath,
      );
      result = await readAndValidateResult(codexResultPath, valCtx);
    }

    // 4. Kit contents pass.
    if (result.is_kit) {
      const confirmed = getConfirmedArticles(result);
      const q = buildKitContentsQuery(article, confirmed);
      const exa = await cachedExaCall(
        "web_search_exa",
        { query: q, numResults: EXA_NUM_RESULTS },
        runId,
      );
      await writeExaFile(exaKitPath, article, q, exa);
      if (!exaResultContainsArticle(exa, article)) {
        throw new NoExactDataError(`Kit contents Exa did not contain exact article "${article}".`);
      }
      await runTurnStreamed(
        thread,
        buildKitContentsPrompt({
          partNumber: article,
          outputJsonPath: codexResultPath,
          kitContentsExaJsonPath: exaKitPath,
          ctx: promptCtx,
        }),
        messagesJsonlPath,
      );
      result = await readAndValidateResult(codexResultPath, valCtx);
    }

    // 5. Решаем итоговый статус.
    const needsReviewReason = detectNeedsReview(result);
    await saveDraftResult(runId, result, needsReviewReason);

    const status: TaskRunStatus = needsReviewReason === null ? "done" : "needs_human_review";
    await finishRun(runId, status, needsReviewReason ?? undefined);
    return { taskId: -1, runId, status, storageDir: storageDirRel };
  } catch (err) {
    if (err instanceof NoExactDataError) {
      await finishRun(runId, "failed_no_data", err.message);
      return { taskId: -1, runId, status: "failed_no_data", storageDir: storageDirRel };
    }
    if (err instanceof ValidationError) {
      await finishRun(runId, "failed_validation", err.message);
      return { taskId: -1, runId, status: "failed_validation", storageDir: storageDirRel };
    }
    await finishRun(runId, "failed_crashed", err instanceof Error ? err.message : String(err));
    throw err;
  }
}

// Совмещённый путь для одноразового CLI (npm run research -- ART): enqueue + execute сразу.
export async function runResearchTask(rawArticle: string): Promise<RunResult> {
  const { runId, article, taskId } = await enqueueResearch(rawArticle);
  const r = await executeRun(runId, article);
  return { ...r, taskId };
}

function detectNeedsReview(r: StructuredResult): string | null {
  if (r.is_kit && Object.keys(r.kit_contents).length === 0) {
    return "kit_without_contents";
  }
  if (r.product_type === null) {
    return "product_type_unknown";
  }
  return null;
}

async function readAndValidateResult(
  path: string,
  ctx: ValidationContext,
): Promise<StructuredResult> {
  try {
    await stat(path);
  } catch {
    throw new ValidationError("agent did not call write_result for this turn");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(await readFile(path, "utf8"));
  } catch (err) {
    throw new ValidationError(`failed to parse codex result: ${msg(err)}`);
  }
  try {
    validateStructuredResult(parsed, ctx);
  } catch (err) {
    throw new ValidationError(msg(err));
  }
  return parsed as StructuredResult;
}

async function writeExaFile(
  absPath: string,
  partNumber: string,
  query: string,
  rawExaResult: unknown,
  extra: Record<string, unknown> = {},
): Promise<void> {
  await writeFile(
    absPath,
    JSON.stringify(
      {
        task_part_number: partNumber,
        tool: "web_search_exa",
        num_results: EXA_NUM_RESULTS,
        query,
        ...extra,
        raw_exa_result: rawExaResult,
      },
      null,
      2,
    ),
  );
}

function msg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
