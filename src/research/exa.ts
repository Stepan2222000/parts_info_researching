// Утилиты для работы с raw Exa-ответом, общие для оркестратора.

export const EXA_NUM_RESULTS = 10;

// Текстовая проверка: содержит ли raw-ответ Exa точную строку артикула.
// Совпадение по подстроке в JSON-репрезентации (как в research_part.ts).
export function exaResultContainsArticle(result: unknown, article: string): boolean {
  return JSON.stringify(result).toLowerCase().includes(article.toLowerCase());
}
