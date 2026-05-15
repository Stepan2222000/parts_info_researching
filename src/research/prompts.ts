// Промпты этапа 2: учитывают brand-маппинг, product_types, Smart-контекст,
// инструмент write_result и общий набор Exa-инструментов.

export type PromptContext = {
  allowedBrands: string[];
  allowedProductTypes: string[];
  brandAliases: Array<{ alias: string; canonical: string }>;
  smartContextMarkdown: string;
  codexRules: string;
};

function brandMappingMarkdown(aliases: Array<{ alias: string; canonical: string }>): string {
  if (aliases.length === 0) return "(маппинг пуст)";
  const lines = ["| Alias | Smart-бренд |", "|---|---|"];
  for (const { alias, canonical } of aliases) {
    lines.push(`| ${alias} | ${canonical} |`);
  }
  return lines.join("\n");
}

function commonHeader(partNumber: string, ctx: PromptContext): string {
  return `
### Допустимые product_type (выбери ОДИН для поля product_type)
${ctx.allowedProductTypes.map((p) => `- ${p}`).join("\n")}

### Допустимые Smart-бренды (используй один или несколько в поле brand_oem)
${ctx.allowedBrands.join(", ")}

### Маппинг алиасов брендов → Smart-бренд
${brandMappingMarkdown(ctx.brandAliases)}

Любой бренд, который ты находишь в источниках, должен быть нормализован в Smart-бренд через этот маппинг. Если бренда нет ни в списке Smart, ни в маппинге — это значит, что мы пока не знаем такого OEM, оставь brand_oem пустым массивом.

${ctx.smartContextMarkdown ? ctx.smartContextMarkdown + "\n" : ""}### Доступные инструменты
- web_search_exa({query, numResults}) — поиск через Exa.
- web_fetch_exa({urls, maxCharacters}) — забрать содержимое страницы как markdown. Использовать, если highlights из web_search_exa недостаточны.
- write_result({json}) — единственный способ сохранить итоговый JSON. Backend сам валидирует и пишет файл.

Помимо обязательных Exa-поисков, заложенных бэкендом, ты можешь делать дополнительные web_search_exa / web_fetch_exa запросы, если этого не хватает. Не зацикливайся — около 20 tool calls на весь thread.

Жесткие правила:
${ctx.codexRules}
`.trim();
}

const SCHEMA_BLOCK = `Строгая JSON-схема результата:
{
  "task_part_number": "<входной артикул>",
  "name": null,
  "brand_oem": [],
  "product_type": null,
  "description": null,
  "weight": null,
  "models": null,
  "is_kit": false,
  "kit_contents": {},
  "part_of_kits": [],
  "numbers": {
    "article": [],
    "article_low_confidence": [],
    "irrelevant": []
  }
}

brand_oem — массив строк из Smart-брендов (например ["MERCRUISER"] или ["VOLVO", "MERCRUISER"]). Если бренд не определён — пустой массив, но это плохой исход, постарайся определить.

product_type — одна из трёх Smart-строк ("Для автомобилей" / "Для мототехники" / "Для водного транспорта"). NULL только если из источников реально нельзя выбрать.

Форматы weight / models / numbers.article / numbers.article_low_confidence / numbers.irrelevant / kit_contents / part_of_kits — как раньше:

{
  "weight": { "kg": 0.123, "source_url": "...", "evidence": "..." }
}
{
  "models": { "text": "Bravo III\\nBravo Two", "source_urls": ["..."], "evidence": "..." }
}
{
  "numbers": {
    "article":            [{ "article": "...", "source_url": "...", "evidence": "..." }],
    "article_low_confidence":[{ "article": "...", "source_url": "...", "evidence": "...", "why_low_confidence": "..." }],
    "irrelevant":         [{ "article": "...", "source_url": "...", "evidence": "...", "why_irrelevant": "..." }]
  }
}
{
  "kit_contents": {
    "<артикул или unknown_N>": {
      "article": "<или null>",
      "name": "...",
      "quantity": 1,
      "description": "...",
      "source_url": "...",
      "evidence": "..."
    }
  }
}
[
  { "kit_article": "<или null>", "kit_name": "...", "source_url": "...", "evidence": "..." }
]

is_kit:
- true, если входной артикул — kit/set/комплект/набор/repair kit/anode kit и подобное.
- false, если одиночная деталь.
- При false kit_contents должен быть пустым объектом {}.

Перед вызовом write_result сам проверь:
- JSON валидный.
- Все обязательные ключи на месте.
- task_part_number равен заданному.
- numbers.article содержит сам task_part_number.
- brand_oem — массив из Smart-брендов (или пустой массив).
- product_type — одна из трёх Smart-строк или null.
- Никаких лишних полей вроде aftermarket/type/confidence.
`;

export function buildExaQuery(partNumber: string): string {
  return `Найди информацию только по точному артикулу "${partNumber}".

Жесткое условие: в каждом полезном источнике строка "${partNumber}" должна явно встречаться в тексте страницы, title, highlights или description. Не используй похожие номера, частичные совпадения, переставленные цифры и номера без буквенного суффикса.

Нужны: точное название детали, OEM-бренд, old/new part number, superseded by, replaces, replacement, cross reference, interchange, fitment/application, kit contents, weight, exploded diagram, parts catalog, PDF, магазины и объявления.

Только OEM. Aftermarket-артикулы не нужны, но если aftermarket-страница явно пишет OEM replacement number "${partNumber}" или другой OEM-номер, можно использовать только OEM-номер.`;
}

export function buildLowConfidenceQuery(partNumber: string, articles: string[]): string {
  return `Проверь, являются ли артикулы ${articles.join(", ")} OEM кросс-номерами, superseded by, replaces, replacement, cross reference или interchange для исходного артикула ${partNumber}. Нужны только OEM связи, aftermarket не нужен. Важно найти источники, где одновременно явно встречаются ${partNumber} и проверяемые артикулы, либо где прямо написана связь между ними. По каждому проверяемому артикулу найди доказательство, что это тот же OEM товар, или доказательство, что связь не подтверждена / это другой товар.`;
}

export function buildKitContentsQuery(partNumber: string, articles: string[]): string {
  return `Найди точный состав OEM набора по исходному артикулу "${partNumber}" и подтвержденным OEM-номерам этого же набора в порядке актуальности: ${articles.join(", ")}. Ищи kit contents, includes, components, component part numbers, quantity, contents list, parts included, exploded diagram, parts catalog, PDF. Жесткое условие: в каждом полезном источнике должен явно встречаться хотя бы один из этих OEM-номеров набора: ${articles.join(", ")}. Нужны артикулы компонентов, названия компонентов, количество каждого компонента и источник, который подтверждает, что компонент входит именно в этот OEM-набор. Aftermarket не нужен.`;
}

export function buildMainPrompt(params: {
  partNumber: string;
  exaJsonPath: string;
  outputJsonPath: string;
  ctx: PromptContext;
}): string {
  return `
Ты исследуешь OEM-запчасть по точному артикулу и собираешь структурированный JSON.

Входной артикул задачи: ${params.partNumber}
Файл с сохранённым ответом основного Exa-поиска: ${params.exaJsonPath}
Файл, куда будет записан итог: ${params.outputJsonPath} (через инструмент write_result, сам файл не пиши)

${commonHeader(params.partNumber, params.ctx)}

${SCHEMA_BLOCK}

Шаги:
1. Прочитай ${params.exaJsonPath}.
2. При необходимости вызови дополнительные web_search_exa / web_fetch_exa.
3. Сформируй итоговый JSON по схеме.
4. Вызови write_result({"json": <итоговый JSON>}).
`.trim();
}

export function buildLowConfidencePrompt(params: {
  partNumber: string;
  outputJsonPath: string;
  lowConfidenceExaJsonPath: string;
  articles: string[];
  ctx: PromptContext;
}): string {
  return `
Продолжи работу с уже созданным JSON. Это уточнение по сомнительным артикулам.

Входной артикул задачи: ${params.partNumber}
Текущий JSON находится в: ${params.outputJsonPath} (читай его оттуда)
Доп. Exa-поиск по сомнительным артикулам: ${params.lowConfidenceExaJsonPath}
Проверяемые артикулы: ${params.articles.join(", ")}

${commonHeader(params.partNumber, params.ctx)}

Задача:
- Прочитай текущий JSON.
- Прочитай дополнительный Exa JSON.
- Один и тот же артикул не должен находиться одновременно в нескольких массивах (article / article_low_confidence / irrelevant).
- Зафиксируй итоговое решение по имеющимся данным.
- Вызови write_result с обновлённым полным JSON.
- Это финальный проход по low-confidence: новых web_search_exa делать не надо.
`.trim();
}

export function buildKitContentsPrompt(params: {
  partNumber: string;
  outputJsonPath: string;
  kitContentsExaJsonPath: string;
  ctx: PromptContext;
}): string {
  return `
Продолжи работу с уже созданным JSON. Это уточнение по составу набора.

Входной артикул задачи: ${params.partNumber}
Текущий JSON: ${params.outputJsonPath}
Доп. Exa-поиск по составу набора: ${params.kitContentsExaJsonPath}

${commonHeader(params.partNumber, params.ctx)}

Задача:
- Прочитай текущий JSON.
- Прочитай дополнительный Exa JSON по составу набора.
- Обнови kit_contents:
  - Если найден артикул компонента — используй его как ключ.
  - Если артикул компонента неизвестен — используй ключи unknown_1, unknown_2 и ставь "article": null. Никогда не пиши пустую строку "" в article.
  - Заполни article, name, quantity (или null), description, source_url, evidence.
- Не трогай numbers.article — туда компоненты не добавляются.
- is_kit должен остаться true.
- Новых web_search_exa не делай, работай по имеющимся данным.
- Вызови write_result с обновлённым полным JSON.
`.trim();
}
