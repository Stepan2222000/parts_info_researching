import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PART_NUMBER = process.argv[2] ?? "21730348";
const NUM_RESULTS = 13;
const EXA_API_KEY = process.env.EXA_API_KEY ?? "";
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));

const tools = ["web_search_exa"].join(",");
const exaUrl = new URL(`https://mcp.exa.ai/mcp?tools=${tools}`);

function buildExaQuery(partNumber: string) {
  return `Найди всю доступную информацию по артикулу ${partNumber}: точное название детали, бренд производитель OEM (ни в коем случае не aftermarket), старые номера old part number, новые номера new part number, superseded by, replaces, replacement, cross reference, interchange, применяемость fitment application к моделям годам двигателям, состав комплекта kit contents если это набор, вес weight, схемы exploded diagram parts catalog, официальные каталоги, магазины, PDF, объявления; важно найти источники где артикул ${partNumber} встречается явно в тексте страницы`;
}

function buildLowConfidenceQuery(partNumber: string, articles: string[]) {
  return `Проверь, являются ли артикулы ${articles.join(", ")} OEM кросс-номерами, superseded by, replaces, replacement, cross reference или interchange для исходного артикула ${partNumber}. Нужны только OEM связи, aftermarket не нужен. Важно найти источники, где одновременно явно встречаются ${partNumber} и проверяемые артикулы, либо где прямо написана связь между ними. По каждому проверяемому артикулу найди доказательство, что это тот же OEM товар, или доказательство, что связь не подтверждена / это другой товар.`;
}

function buildCodexPrompt(params: {
  partNumber: string;
  exaJsonPath: string;
  outputJsonPath: string;
}) {
  return `
Ты отдельный Codex-агент для структурирования результата поиска по OEM-запчасти.

Входной артикул задачи: ${params.partNumber}
Файл с сохраненным сырым ответом Exa: ${params.exaJsonPath}
Файл, который ты обязан создать: ${params.outputJsonPath}

Жесткие правила:
- Используй только данные из файла Exa JSON. В интернет не ходи. Никаких fetch, web search, MCP, браузера.
- Ответ Exa не переводи и не меняй. Он уже сохранен отдельно.
- Создай только JSON-файл по указанному пути. Никаких markdown-файлов, txt-файлов или дополнительных отчетов.
- Итоговый JSON должен быть на русском языке.
- Не придумывай данные. Если поле не подтверждено Exa-сниппетами, ставь null, пустую строку, пустой массив или пустой объект по схеме.
- Aftermarket не нужен. Не добавляй поле aftermarket. OEM-артикулы только от производителя/официальной OEM-линейки.
- Для Mercury/MerCruiser/Quicksilver/Mariner считай OEM-брендом "Mercury Marine".
- Вес всегда указывай в килограммах. Если вес найден в фунтах/унциях/граммах, переведи в кг. Если вес не найден, weight = null.
- numbers.article отсортируй так: сначала самые новые/актуальные OEM-артикулы, потом более старые. Не добавляй type/confidence.
- numbers.article_low_confidence используй для OEM-кандидатов, где связи не хватает для уверенного вывода.
- numbers.irrelevant добавляй только если есть реально отброшенные номера; для каждого нужен источник и обоснование.
- Каждый article, article_low_confidence и irrelevant должен иметь source_url и evidence на русском: что в источнике написано и почему это доказывает или не доказывает связь.
- models это объект с text/source_urls/evidence: в text перечисли применяемость по моделям с каждой новой строки. Если моделей нет или источники слабые, models = null.
- kit_contents это объект. Внутри по каждой запчасти комплекта укажи article, name, quantity, description, source_url, evidence. Если артикул компонента неизвестен, ключ "unknown_1", "unknown_2".
- part_of_kits используй, если искомая запчасть входит в наборы.

Строгая JSON-схема результата:
{
  "task_part_number": "${params.partNumber}",
  "name": null,
  "brand_oem": null,
  "description": null,
  "weight": null,
  "models": null,
  "kit_contents": {},
  "part_of_kits": [],
  "numbers": {
    "article": [],
    "article_low_confidence": [],
    "irrelevant": []
  }
}

Формат models, если найдена применяемость:
{
  "text": "Bravo III\\nBravo Two\\nBravo X Three",
  "source_urls": ["https://...", "https://..."],
  "evidence": "В источниках перечислены применимые модели ..."
}

Если применяемость не подтверждена источниками, models = null.

Формат weight, если найден:
{
  "kg": 0.123,
  "source_url": "https://...",
  "evidence": "В источнике указан вес ..., это переведено в кг."
}

Формат numbers.article:
{
  "article": "8M0077471",
  "source_url": "https://...",
  "evidence": "В источнике написано ..., это подтверждает OEM-связь с ${params.partNumber}."
}

Формат numbers.article_low_confidence:
{
  "article": "879984A1",
  "source_url": "https://...",
  "evidence": "В источнике номер найден рядом с ${params.partNumber}, но нет явной фразы superseded/replaces/interchange.",
  "why_low_confidence": "Нужна дополнительная проверка."
}

Формат numbers.irrelevant:
{
  "article": "18-6154M",
  "source_url": "https://...",
  "evidence": "В источнике это указано как Sierra/aftermarket или как другой товар.",
  "why_irrelevant": "Это не OEM-кросс-номер искомой детали."
}

Формат kit_contents:
{
  "123456": {
    "article": "123456",
    "name": "Название компонента",
    "quantity": 1,
    "description": "Что это за запчасть",
    "source_url": "https://...",
    "evidence": "В источнике указано, что этот компонент входит в комплект."
  }
}

Формат part_of_kits:
[
  {
    "kit_article": "123456",
    "kit_name": "Название набора",
    "source_url": "https://...",
    "evidence": "В источнике указано, что ${params.partNumber} входит в этот набор."
  }
]

Перед записью проверь:
- JSON валидный.
- Обязательные верхние ключи есть по схеме.
- Нет поля aftermarket.
- Нет type/confidence у артикулов.
- task_part_number равен "${params.partNumber}".

Запиши итоговый JSON в файл: ${params.outputJsonPath}
`.trim();
}

function buildLowConfidencePrompt(params: {
  partNumber: string;
  outputJsonPath: string;
  lowConfidenceExaJsonPath: string;
  articles: string[];
}) {
  return `
Продолжи работу с уже созданным JSON по OEM-запчасти.

Входной артикул задачи: ${params.partNumber}
Текущий структурированный JSON: ${params.outputJsonPath}
Файл с дополнительным Exa research по сомнительным артикулам: ${params.lowConfidenceExaJsonPath}
Проверяемые article_low_confidence: ${params.articles.join(", ")}

Задача:
- Прочитай текущий структурированный JSON.
- Прочитай дополнительный Exa JSON.
- Обнови тот же файл: ${params.outputJsonPath}

Правила обновления:
- Используй только текущий JSON и дополнительный Exa JSON. В интернет не ходи. Никаких fetch, web search, MCP, браузера.
- Если дополнительный Exa research явно подтвердил OEM-связь проверяемого артикула с ${params.partNumber}, перенеси его из numbers.article_low_confidence в numbers.article.
- Если дополнительный Exa research не подтвердил связь, оставь артикул в numbers.article_low_confidence и обнови evidence / why_low_confidence.
- Если дополнительный Exa research доказал, что артикул не относится к искомой OEM-детали или является aftermarket/другим товаром, перенеси его в numbers.irrelevant.
- Один и тот же артикул не должен одновременно находиться в нескольких массивах.
- Если в дополнительном Exa research найден новый OEM-артикул с явной связью superseded/replaces/interchange/cross reference, добавь его в numbers.article. Если связь слабая, добавь в numbers.article_low_confidence.
- numbers.article отсортируй так: сначала самые новые/актуальные OEM-артикулы, потом более старые.
- Не добавляй верхнее поле sources.
- Не добавляй поле aftermarket.
- Не добавляй type/confidence у артикулов.
- Итоговый JSON должен остаться на русском языке и соответствовать той же схеме.
- Запиши обновленный JSON в тот же файл: ${params.outputJsonPath}
`.trim();
}

function assertObject(value: unknown, name: string): asserts value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
}

function assertArray(value: unknown, name: string): asserts value is unknown[] {
  if (!Array.isArray(value)) {
    throw new Error(`${name} must be an array`);
  }
}

function assertRequiredKeys(
  value: Record<string, unknown>,
  requiredKeys: string[],
  name: string,
) {
  for (const key of requiredKeys) {
    if (!(key in value)) {
      throw new Error(`Missing key in ${name}: ${key}`);
    }
  }
}

function assertString(value: unknown, name: string): asserts value is string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${name} must be a non-empty string`);
  }
}

function validateArticleArray(
  value: unknown[],
  name: "numbers.article" | "numbers.article_low_confidence" | "numbers.irrelevant",
) {
  for (const [index, item] of value.entries()) {
    const itemName = `${name}[${index}]`;
    assertObject(item, itemName);

    assertString(item.article, `${itemName}.article`);
    assertString(item.source_url, `${itemName}.source_url`);
    assertString(item.evidence, `${itemName}.evidence`);

    if (name === "numbers.article_low_confidence") {
      assertString(item.why_low_confidence, `${itemName}.why_low_confidence`);
    }

    if (name === "numbers.irrelevant") {
      assertString(item.why_irrelevant, `${itemName}.why_irrelevant`);
    }
  }
}

function validateKitContents(value: Record<string, unknown>) {
  for (const [key, item] of Object.entries(value)) {
    const itemName = `kit_contents.${key}`;
    assertObject(item, itemName);
    assertRequiredKeys(
      item,
      ["article", "name", "quantity", "description", "source_url", "evidence"],
      itemName,
    );

    if (item.article !== null) {
      assertString(item.article, `${itemName}.article`);
    }
    if (item.name !== null) {
      assertString(item.name, `${itemName}.name`);
    }
    if (item.quantity !== null && typeof item.quantity !== "number") {
      throw new Error(`${itemName}.quantity must be a number or null`);
    }
    if (item.description !== null) {
      assertString(item.description, `${itemName}.description`);
    }

    assertString(item.source_url, `${itemName}.source_url`);
    assertString(item.evidence, `${itemName}.evidence`);
  }
}

function validatePartOfKits(value: unknown[]) {
  for (const [index, item] of value.entries()) {
    const itemName = `part_of_kits[${index}]`;
    assertObject(item, itemName);
    assertRequiredKeys(item, ["kit_article", "kit_name", "source_url", "evidence"], itemName);
    assertString(item.kit_article, `${itemName}.kit_article`);
    assertString(item.kit_name, `${itemName}.kit_name`);
    assertString(item.source_url, `${itemName}.source_url`);
    assertString(item.evidence, `${itemName}.evidence`);
  }
}

function validateStructuredResult(value: unknown) {
  assertObject(value, "codex result");

  const requiredTopLevel = [
    "task_part_number",
    "name",
    "brand_oem",
    "description",
    "weight",
    "models",
    "kit_contents",
    "part_of_kits",
    "numbers",
  ];

  assertRequiredKeys(value, requiredTopLevel, "codex result");

  if (value.task_part_number !== PART_NUMBER) {
    throw new Error(
      `task_part_number must be ${PART_NUMBER}, got ${String(value.task_part_number)}`,
    );
  }

  assertObject(value.kit_contents, "kit_contents");
  assertArray(value.part_of_kits, "part_of_kits");
  assertObject(value.numbers, "numbers");
  validateKitContents(value.kit_contents);
  validatePartOfKits(value.part_of_kits);

  if (value.models !== null) {
    assertObject(value.models, "models");
    assertRequiredKeys(value.models, ["text", "source_urls", "evidence"], "models");
    assertString(value.models.text, "models.text");
    assertArray(value.models.source_urls, "models.source_urls");

    if (value.models.source_urls.length === 0) {
      throw new Error("models.source_urls must not be empty");
    }

    for (const [index, sourceUrl] of value.models.source_urls.entries()) {
      assertString(sourceUrl, `models.source_urls[${index}]`);
    }

    assertString(value.models.evidence, "models.evidence");
  }

  if (value.weight !== null) {
    assertObject(value.weight, "weight");
    assertRequiredKeys(value.weight, ["kg", "source_url", "evidence"], "weight");

    if (typeof value.weight.kg !== "number") {
      throw new Error("weight.kg must be a number");
    }

    assertString(value.weight.source_url, "weight.source_url");
    assertString(value.weight.evidence, "weight.evidence");
  }

  const numbers = value.numbers;
  assertRequiredKeys(numbers, ["article", "article_low_confidence", "irrelevant"], "numbers");
  assertArray(numbers.article, "numbers.article");
  assertArray(numbers.article_low_confidence, "numbers.article_low_confidence");
  assertArray(numbers.irrelevant, "numbers.irrelevant");

  validateArticleArray(numbers.article, "numbers.article");
  validateArticleArray(numbers.article_low_confidence, "numbers.article_low_confidence");
  validateArticleArray(numbers.irrelevant, "numbers.irrelevant");
}

async function callExaSearch(query: string) {
  const exaClient = new Client({
    name: "part-research-agent",
    version: "1.0.0",
  });

  const exaTransport = new StreamableHTTPClientTransport(exaUrl, {
    requestInit: {
      headers: {
        "x-api-key": EXA_API_KEY,
      },
    },
  });

  await exaClient.connect(exaTransport);
  try {
    return await exaClient.callTool({
      name: "web_search_exa",
      arguments: {
        query,
        numResults: NUM_RESULTS,
      },
    });
  } finally {
    await exaTransport.close();
  }
}

function getLowConfidenceArticles(value: unknown): string[] {
  validateStructuredResult(value);
  assertObject(value, "codex result");
  assertObject(value.numbers, "numbers");
  assertArray(value.numbers.article_low_confidence, "numbers.article_low_confidence");

  return value.numbers.article_low_confidence.map((item, index) => {
    assertObject(item, `numbers.article_low_confidence[${index}]`);
    const article = item.article;
    assertString(article, `numbers.article_low_confidence[${index}].article`);
    return article;
  });
}

async function main() {
  if (!EXA_API_KEY) {
    throw new Error("EXA_API_KEY is empty");
  }

  const exaDir = resolve(SCRIPT_DIR, "exa_results");
  const codexDir = resolve(SCRIPT_DIR, "codex_results");
  const exaJsonPath = resolve(exaDir, `${PART_NUMBER}.json`);
  const lowConfidenceExaJsonPath = resolve(exaDir, `${PART_NUMBER}_low_confidence_check.json`);
  const outputJsonPath = resolve(codexDir, `${PART_NUMBER}.json`);

  await mkdir(exaDir, { recursive: true });
  await mkdir(codexDir, { recursive: true });

  const query = buildExaQuery(PART_NUMBER);
  const exaResult = await callExaSearch(query);

  await writeFile(
    exaJsonPath,
    JSON.stringify(
      {
        task_part_number: PART_NUMBER,
        tool: "web_search_exa",
        num_results: NUM_RESULTS,
        query,
        raw_exa_result: exaResult,
      },
      null,
      2,
    ),
  );

  const prompt = buildCodexPrompt({
    partNumber: PART_NUMBER,
    exaJsonPath,
    outputJsonPath,
  });

  const { Codex } = await import("@openai/codex-sdk");
  const codex = new Codex();
  const thread = codex.startThread({
    workingDirectory: process.cwd(),
    skipGitRepoCheck: true,
    sandboxMode: "workspace-write",
    approvalPolicy: "never",
    networkAccessEnabled: false,
    webSearchMode: "disabled",
  });

  await thread.run(prompt);

  const structuredText = await readFile(outputJsonPath, "utf8");
  const structuredResult = JSON.parse(structuredText) as unknown;
  validateStructuredResult(structuredResult);

  const lowConfidenceArticles = getLowConfidenceArticles(structuredResult);

  if (lowConfidenceArticles.length > 0) {
    const lowConfidenceQuery = buildLowConfidenceQuery(PART_NUMBER, lowConfidenceArticles);
    const lowConfidenceExaResult = await callExaSearch(lowConfidenceQuery);

    await writeFile(
      lowConfidenceExaJsonPath,
      JSON.stringify(
        {
          task_part_number: PART_NUMBER,
          tool: "web_search_exa",
          num_results: NUM_RESULTS,
          query: lowConfidenceQuery,
          checked_articles: lowConfidenceArticles,
          raw_exa_result: lowConfidenceExaResult,
        },
        null,
        2,
      ),
    );

    await thread.run(
      buildLowConfidencePrompt({
        partNumber: PART_NUMBER,
        outputJsonPath,
        lowConfidenceExaJsonPath,
        articles: lowConfidenceArticles,
      }),
    );

    const updatedStructuredText = await readFile(outputJsonPath, "utf8");
    const updatedStructuredResult = JSON.parse(updatedStructuredText) as unknown;
    validateStructuredResult(updatedStructuredResult);
  }

  console.log(`PART_NUMBER: ${PART_NUMBER}`);
  console.log(`Exa raw saved: ${exaJsonPath}`);
  if (lowConfidenceArticles.length > 0) {
    console.log(`Low confidence Exa raw saved: ${lowConfidenceExaJsonPath}`);
  } else {
    console.log("No low confidence articles to verify.");
  }
  console.log(`Codex JSON saved: ${outputJsonPath}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
