// Валидация структурного JSON от research-агента.
// Логика максимально близка к research_part.ts — это перенос без переработки.

export type ArticleItem = {
  article: string;
  source_url: string;
  evidence: string;
};

export type LowConfidenceItem = ArticleItem & { why_low_confidence: string };
export type IrrelevantItem = ArticleItem & { why_irrelevant: string };

export type WeightBlock = { kg: number; source_url: string; evidence: string };
export type ModelsBlock = { text: string; source_urls: string[]; evidence: string };

export type KitComponent = {
  article: string | null;
  name: string | null;
  quantity: number | null;
  description: string | null;
  source_url: string;
  evidence: string;
};

export type PartOfKit = {
  kit_article: string | null;
  kit_name: string;
  source_url: string;
  evidence: string;
};

export type StructuredResult = {
  task_part_number: string;
  name: string | null;
  brand_oem: string | null;
  description: string | null;
  weight: WeightBlock | null;
  models: ModelsBlock | null;
  is_kit: boolean;
  kit_contents: Record<string, KitComponent>;
  part_of_kits: PartOfKit[];
  numbers: {
    article: ArticleItem[];
    article_low_confidence: LowConfidenceItem[];
    irrelevant: IrrelevantItem[];
  };
};

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

function assertString(value: unknown, name: string): asserts value is string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${name} must be a non-empty string`);
  }
}

function assertRequiredKeys(
  value: Record<string, unknown>,
  keys: string[],
  name: string,
): void {
  for (const key of keys) {
    if (!(key in value)) {
      throw new Error(`Missing key in ${name}: ${key}`);
    }
  }
}

function samePartNumber(a: string, b: string): boolean {
  return a.trim().toLowerCase() === b.trim().toLowerCase();
}

function findMatchingPartNumber(value: string, partNumbers: string[]): string | null {
  return partNumbers.find((p) => samePartNumber(value, p)) ?? null;
}

function validateArticleList(
  value: unknown[],
  name: "numbers.article" | "numbers.article_low_confidence" | "numbers.irrelevant",
): void {
  for (const [i, item] of value.entries()) {
    const itemName = `${name}[${i}]`;
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

function validateKitContents(
  value: Record<string, unknown>,
  ownPartNumbers: string[],
): void {
  for (const [key, item] of Object.entries(value)) {
    const itemName = `kit_contents.${key}`;
    const matchingKey = findMatchingPartNumber(key, ownPartNumbers);
    if (matchingKey !== null) {
      throw new Error(`${itemName} must not use own part article ${matchingKey} as a kit component`);
    }
    assertObject(item, itemName);
    assertRequiredKeys(
      item,
      ["article", "name", "quantity", "description", "source_url", "evidence"],
      itemName,
    );

    if (item.article !== null) {
      assertString(item.article, `${itemName}.article`);
      const matchingArticle = findMatchingPartNumber(item.article, ownPartNumbers);
      if (matchingArticle !== null) {
        throw new Error(
          `${itemName}.article must not equal own part article ${matchingArticle}`,
        );
      }
    }
    if (item.name !== null) assertString(item.name, `${itemName}.name`);
    if (item.quantity !== null && typeof item.quantity !== "number") {
      throw new Error(`${itemName}.quantity must be a number or null`);
    }
    if (item.description !== null) assertString(item.description, `${itemName}.description`);

    assertString(item.source_url, `${itemName}.source_url`);
    assertString(item.evidence, `${itemName}.evidence`);
  }
}

function validatePartOfKits(value: unknown[]): void {
  for (const [i, item] of value.entries()) {
    const name = `part_of_kits[${i}]`;
    assertObject(item, name);
    assertRequiredKeys(item, ["kit_article", "kit_name", "source_url", "evidence"], name);
    if (item.kit_article !== null) assertString(item.kit_article, `${name}.kit_article`);
    assertString(item.kit_name, `${name}.kit_name`);
    assertString(item.source_url, `${name}.source_url`);
    assertString(item.evidence, `${name}.evidence`);
  }
}

export function validateStructuredResult(
  value: unknown,
  expectedPartNumber: string,
): asserts value is StructuredResult {
  assertObject(value, "codex result");

  assertRequiredKeys(
    value,
    [
      "task_part_number",
      "name",
      "brand_oem",
      "description",
      "weight",
      "models",
      "is_kit",
      "kit_contents",
      "part_of_kits",
      "numbers",
    ],
    "codex result",
  );

  if (value.task_part_number !== expectedPartNumber) {
    throw new Error(
      `task_part_number must be ${expectedPartNumber}, got ${String(value.task_part_number)}`,
    );
  }

  assertObject(value.kit_contents, "kit_contents");
  assertArray(value.part_of_kits, "part_of_kits");
  assertObject(value.numbers, "numbers");

  if (typeof value.is_kit !== "boolean") throw new Error("is_kit must be a boolean");
  if (value.is_kit === false && Object.keys(value.kit_contents).length > 0) {
    throw new Error("kit_contents must be empty when is_kit is false");
  }

  validatePartOfKits(value.part_of_kits);

  if (value.models !== null) {
    assertObject(value.models, "models");
    assertRequiredKeys(value.models, ["text", "source_urls", "evidence"], "models");
    assertString(value.models.text, "models.text");
    assertArray(value.models.source_urls, "models.source_urls");
    if (value.models.source_urls.length === 0) {
      throw new Error("models.source_urls must not be empty");
    }
    for (const [i, url] of value.models.source_urls.entries()) {
      assertString(url, `models.source_urls[${i}]`);
    }
    assertString(value.models.evidence, "models.evidence");
  }

  if (value.weight !== null) {
    assertObject(value.weight, "weight");
    assertRequiredKeys(value.weight, ["kg", "source_url", "evidence"], "weight");
    if (typeof value.weight.kg !== "number") throw new Error("weight.kg must be a number");
    assertString(value.weight.source_url, "weight.source_url");
    assertString(value.weight.evidence, "weight.evidence");
  }

  const numbers = value.numbers;
  assertRequiredKeys(numbers, ["article", "article_low_confidence", "irrelevant"], "numbers");
  assertArray(numbers.article, "numbers.article");
  assertArray(numbers.article_low_confidence, "numbers.article_low_confidence");
  assertArray(numbers.irrelevant, "numbers.irrelevant");

  validateArticleList(numbers.article, "numbers.article");
  validateArticleList(numbers.article_low_confidence, "numbers.article_low_confidence");
  validateArticleList(numbers.irrelevant, "numbers.irrelevant");

  const ownPartNumbers = numbers.article.map((item, i) => {
    assertObject(item, `numbers.article[${i}]`);
    const a = item.article;
    assertString(a, `numbers.article[${i}].article`);
    return a;
  });

  const hasTaskPN = ownPartNumbers.some((a) => samePartNumber(a, expectedPartNumber));
  if (!hasTaskPN) {
    throw new Error(`numbers.article must include task_part_number ${expectedPartNumber}`);
  }

  validateKitContents(value.kit_contents, ownPartNumbers);
}

export function getLowConfidenceArticles(result: StructuredResult): string[] {
  return [...new Set(result.numbers.article_low_confidence.map((a) => a.article))];
}

export function getConfirmedArticles(result: StructuredResult): string[] {
  return result.numbers.article.map((a) => a.article);
}
