// Промпты и запросы к Exa/Codex — перенос из research_part.ts один-в-один.

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

export function buildCodexPrompt(params: {
  partNumber: string;
  exaJsonPath: string;
  outputJsonPath: string;
  codexRules: string;
}): string {
  return `
Ты отдельный Codex-агент для структурирования результата поиска по OEM-запчасти.

Входной артикул задачи: ${params.partNumber}
Файл с сохраненным сырым ответом Exa: ${params.exaJsonPath}
Файл, который ты обязан создать: ${params.outputJsonPath}

Жесткие правила:
${params.codexRules}

Строгая JSON-схема результата:
{
  "task_part_number": "${params.partNumber}",
  "name": null,
  "brand_oem": null,
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
    "article": "123456 или null",
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

Поле is_kit:
- true, если входной артикул сам является набором/комплектом.
- false, если входной артикул одиночная деталь или только входит в чужой набор.
- Если is_kit = false, kit_contents должен быть пустым объектом.

Перед записью проверь:
- JSON валидный.
- Обязательные верхние ключи есть по схеме.
- Нет поля aftermarket.
- Нет type/confidence у артикулов.
- task_part_number равен "${params.partNumber}".
- numbers.article содержит task_part_number "${params.partNumber}" с источником и evidence.

Запиши итоговый JSON в файл: ${params.outputJsonPath}
`.trim();
}

export function buildLowConfidencePrompt(params: {
  partNumber: string;
  outputJsonPath: string;
  lowConfidenceExaJsonPath: string;
  articles: string[];
  codexRules: string;
}): string {
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
${params.codexRules}
- Один и тот же артикул не должен одновременно находиться в нескольких массивах.
- Это второй и последний проход уточнения: не делай новых запросов, зафиксируй итоговое решение по имеющимся данным.
- Запиши обновленный JSON в тот же файл: ${params.outputJsonPath}
`.trim();
}

export function buildKitContentsPrompt(params: {
  partNumber: string;
  outputJsonPath: string;
  kitContentsExaJsonPath: string;
  codexRules: string;
}): string {
  return `
Продолжи работу с уже созданным JSON по OEM-набору.

Входной артикул задачи: ${params.partNumber}
Текущий структурированный JSON: ${params.outputJsonPath}
Файл с дополнительным Exa research по составу набора: ${params.kitContentsExaJsonPath}

Задача:
- Прочитай текущий структурированный JSON.
- Прочитай дополнительный Exa JSON по составу набора.
- Обнови kit_contents в том же файле: ${params.outputJsonPath}

Правила обновления:
${params.codexRules}
- Это дополнительный проход только по составу набора. Не добавляй компоненты набора в numbers.article.
- Если найден артикул компонента, используй его ключом kit_contents. Если артикул компонента неизвестен, используй unknown_1, unknown_2 и ставь article: null.
- По каждому компоненту заполни article, name, quantity, description, source_url, evidence.
- Никогда не ставь пустую строку "" в article компонента.
- Если количество не найдено, quantity = null.
- Если дополнительный Exa research не дал нового состава, оставь kit_contents как есть.
- is_kit должен остаться true.
- Не делай новых запросов.
- Запиши обновленный JSON в тот же файл: ${params.outputJsonPath}
`.trim();
}
