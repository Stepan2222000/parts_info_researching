// Валидация структурного JSON от research-агента (этап 2).

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
  brand_oem: string[];
  product_type: string | null;
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

export type ValidationContext = {
  expectedPartNumber: string;
  allowedBrands: string[];
  allowedProductTypes: string[];
};

function assertObject(v: unknown, name: string): asserts v is Record<string, unknown> {
  if (typeof v !== "object" || v === null || Array.isArray(v)) {
    throw new Error(`${name} must be an object`);
  }
}

function assertArray(v: unknown, name: string): asserts v is unknown[] {
  if (!Array.isArray(v)) throw new Error(`${name} must be an array`);
}

function assertString(v: unknown, name: string): asserts v is string {
  if (typeof v !== "string" || v.trim() === "") {
    throw new Error(`${name} must be a non-empty string`);
  }
}

function assertRequiredKeys(o: Record<string, unknown>, keys: string[], name: string): void {
  for (const k of keys) if (!(k in o)) throw new Error(`Missing key in ${name}: ${k}`);
}

function samePartNumber(a: string, b: string): boolean {
  return a.trim().toLowerCase() === b.trim().toLowerCase();
}

function findMatching(value: string, list: string[]): string | null {
  return list.find((p) => samePartNumber(value, p)) ?? null;
}

function validateArticleList(
  list: unknown[],
  name: "numbers.article" | "numbers.article_low_confidence" | "numbers.irrelevant",
): void {
  for (const [i, item] of list.entries()) {
    const n = `${name}[${i}]`;
    assertObject(item, n);
    assertString(item.article, `${n}.article`);
    assertString(item.source_url, `${n}.source_url`);
    assertString(item.evidence, `${n}.evidence`);
    if (name === "numbers.article_low_confidence") {
      assertString(item.why_low_confidence, `${n}.why_low_confidence`);
    }
    if (name === "numbers.irrelevant") {
      assertString(item.why_irrelevant, `${n}.why_irrelevant`);
    }
  }
}

function validateKitContents(value: Record<string, unknown>, ownPartNumbers: string[]): void {
  for (const [key, item] of Object.entries(value)) {
    const n = `kit_contents.${key}`;
    const conflictKey = findMatching(key, ownPartNumbers);
    if (conflictKey !== null) {
      throw new Error(`${n} must not use own part article ${conflictKey} as a kit component`);
    }
    assertObject(item, n);
    assertRequiredKeys(
      item,
      ["article", "name", "quantity", "description", "source_url", "evidence"],
      n,
    );
    if (item.article !== null) {
      assertString(item.article, `${n}.article`);
      const conflictArt = findMatching(item.article, ownPartNumbers);
      if (conflictArt !== null) {
        throw new Error(`${n}.article must not equal own part article ${conflictArt}`);
      }
    }
    if (item.name !== null) assertString(item.name, `${n}.name`);
    if (item.quantity !== null && typeof item.quantity !== "number") {
      throw new Error(`${n}.quantity must be a number or null`);
    }
    if (item.description !== null) assertString(item.description, `${n}.description`);
    assertString(item.source_url, `${n}.source_url`);
    assertString(item.evidence, `${n}.evidence`);
  }
}

function validatePartOfKits(list: unknown[]): void {
  for (const [i, item] of list.entries()) {
    const n = `part_of_kits[${i}]`;
    assertObject(item, n);
    assertRequiredKeys(item, ["kit_article", "kit_name", "source_url", "evidence"], n);
    if (item.kit_article !== null) assertString(item.kit_article, `${n}.kit_article`);
    assertString(item.kit_name, `${n}.kit_name`);
    assertString(item.source_url, `${n}.source_url`);
    assertString(item.evidence, `${n}.evidence`);
  }
}

export function validateStructuredResult(
  value: unknown,
  ctx: ValidationContext,
): asserts value is StructuredResult {
  assertObject(value, "codex result");
  assertRequiredKeys(
    value,
    [
      "task_part_number",
      "name",
      "brand_oem",
      "product_type",
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

  if (value.task_part_number !== ctx.expectedPartNumber) {
    throw new Error(
      `task_part_number must be ${ctx.expectedPartNumber}, got ${String(value.task_part_number)}`,
    );
  }

  // brand_oem: массив строк, каждая — один из allowedBrands.
  assertArray(value.brand_oem, "brand_oem");
  for (const [i, b] of value.brand_oem.entries()) {
    assertString(b, `brand_oem[${i}]`);
    if (!ctx.allowedBrands.includes(b)) {
      throw new Error(
        `brand_oem[${i}]=${b} is not in Smart brands list. Allowed: ${ctx.allowedBrands.join(", ")}`,
      );
    }
  }

  // product_type: string | null, либо одно из allowedProductTypes.
  if (value.product_type !== null) {
    assertString(value.product_type, "product_type");
    if (!ctx.allowedProductTypes.includes(value.product_type)) {
      throw new Error(
        `product_type=${value.product_type} not in Smart product_types: ${ctx.allowedProductTypes.join(" | ")}`,
      );
    }
  }

  if (typeof value.is_kit !== "boolean") throw new Error("is_kit must be a boolean");
  assertObject(value.kit_contents, "kit_contents");
  assertArray(value.part_of_kits, "part_of_kits");
  assertObject(value.numbers, "numbers");

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

  if (!ownPartNumbers.some((a) => samePartNumber(a, ctx.expectedPartNumber))) {
    throw new Error(`numbers.article must include task_part_number ${ctx.expectedPartNumber}`);
  }

  validateKitContents(value.kit_contents, ownPartNumbers);
}

export function getLowConfidenceArticles(result: StructuredResult): string[] {
  return [...new Set(result.numbers.article_low_confidence.map((a) => a.article))];
}

export function getConfirmedArticles(result: StructuredResult): string[] {
  return result.numbers.article.map((a) => a.article);
}
