import type { PoolClient } from "pg";
import { pool } from "../../db/pool.js";
import { recordPublication } from "../../db/curator.js";

// Спецификация — см. save_to_smart.md в корне репо.

export type SaveComponent = {
  smart_id?: string;
  name?: string;
  articles?: string[];
  product_type?: string;
  weight_kg?: number;
  model?: string;
  description?: string;
  brands?: string[];
  quantity?: number;
};

export type SavePart = {
  run_id: number;
  smart_id?: string;
  name?: string;
  articles?: string[];
  product_type?: string;
  weight_kg?: number;
  model?: string;
  description?: string;
  brands?: string[];
  components?: SaveComponent[];
};

export type ComponentResult =
  | { index: number; status: "ok"; smart_id: string; linked: true }
  | { index: number; status: "error"; error: string };

export type PartResult =
  | {
      part_index: number;
      status: "ok";
      smart_id: string;
      components?: ComponentResult[];
    }
  | { part_index: number; status: "error"; error: string };

export async function saveToSmart(
  sessionId: number,
  parts: SavePart[],
): Promise<PartResult[]> {
  const results: PartResult[] = [];
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    for (const [idx, part] of parts.entries()) {
      const sp = `part_${idx}`;
      await client.query(`SAVEPOINT ${sp}`);
      try {
        const partResult = await processPart(client, sessionId, idx, part);
        await client.query(`RELEASE SAVEPOINT ${sp}`);
        results.push(partResult);
      } catch (err) {
        await client.query(`ROLLBACK TO SAVEPOINT ${sp}`);
        results.push({
          part_index: idx,
          status: "error",
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }
    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK").catch(() => {});
    throw err;
  } finally {
    client.release();
  }
  return results;
}

async function processPart(
  client: PoolClient,
  sessionId: number,
  partIndex: number,
  part: SavePart,
): Promise<PartResult> {
  let parentId: string;

  if (part.smart_id) {
    const existing = await readPartState(client, part.smart_id);
    if (existing === null) {
      throw new Error(`smart_id=${part.smart_id} not found in smart.parts`);
    }
    if (!existing.is_draft) {
      throw new Error(
        `smart_id=${part.smart_id} is finalized (is_draft=false); cannot overwrite, unfreeze first via execute_sql`,
      );
    }
    if (!existing.is_unverified) {
      throw new Error(
        `smart_id=${part.smart_id} is verified (is_unverified=false); composition is frozen, set is_unverified=true first via execute_sql`,
      );
    }
    parentId = part.smart_id;
    await updateParentFields(client, parentId, part);
    if (part.brands !== undefined) {
      await client.query("DELETE FROM smart.part_brands WHERE part_id = $1", [parentId]);
      await insertBrands(client, parentId, part.brands);
    }
  } else {
    parentId = await insertNewPart(client, part);
    await insertBrands(client, parentId, part.brands ?? []);
  }

  let compResults: ComponentResult[] | undefined;
  if (part.components !== undefined) {
    if (part.smart_id) {
      await client.query("DELETE FROM smart.part_components WHERE parent_id = $1", [parentId]);
    }
    compResults = await processComponents(client, parentId, part.components);
  }

  await recordPublication(client, part.run_id, sessionId, parentId);

  if (compResults !== undefined) {
    return { part_index: partIndex, status: "ok", smart_id: parentId, components: compResults };
  }
  return { part_index: partIndex, status: "ok", smart_id: parentId };
}

async function processComponents(
  client: PoolClient,
  parentId: string,
  components: SaveComponent[],
): Promise<ComponentResult[]> {
  const results: ComponentResult[] = [];
  for (const [i, comp] of components.entries()) {
    let childId: string;
    if (comp.smart_id) {
      const existing = await readPartState(client, comp.smart_id);
      if (existing === null) {
        throw new Error(`component[${i}]: smart_id=${comp.smart_id} not found in smart.parts`);
      }
      childId = comp.smart_id;
      if (existing.is_draft) {
        await patchMergeComponent(client, childId, comp);
      }
    } else {
      childId = await insertNewPart(client, comp);
      await insertBrands(client, childId, comp.brands ?? []);
    }
    const qty = comp.quantity ?? 1;
    await client.query(
      `INSERT INTO smart.part_components (parent_id, child_id, quantity, can_be_sold_separately)
       VALUES ($1, $2, $3, false)`,
      [parentId, childId, qty],
    );
    results.push({ index: i, status: "ok", smart_id: childId, linked: true });
  }
  return results;
}

async function readPartState(
  client: PoolClient,
  smartId: string,
): Promise<{ is_draft: boolean; is_unverified: boolean } | null> {
  const { rows } = await client.query<{ is_draft: boolean; is_unverified: boolean }>(
    "SELECT is_draft, is_unverified FROM smart.parts WHERE id = $1",
    [smartId],
  );
  return rows[0] ?? null;
}

// FDW не применяет remote DEFAULTs — отправляем все NOT NULL колонки явно.
// name и product_type обязательны на стороне Smart; если не переданы — будет
// NOT NULL violation, которое чисто откатится через SAVEPOINT.
async function insertNewPart(
  client: PoolClient,
  fields: SavePart | SaveComponent,
): Promise<string> {
  const { rows } = await client.query<{ id: string }>(
    `INSERT INTO smart.parts
       (name, articles, product_type, weight_kg, model, description, is_draft, is_unverified)
     VALUES ($1, $2, $3, $4, $5, $6, true, true)
     RETURNING id`,
    [
      fields.name ?? null,
      fields.articles ?? null,
      fields.product_type ?? null,
      fields.weight_kg ?? null,
      fields.model ?? null,
      fields.description ?? null,
    ],
  );
  return rows[0]!.id;
}

async function updateParentFields(
  client: PoolClient,
  partId: string,
  part: SavePart,
): Promise<void> {
  const set: string[] = [];
  const params: unknown[] = [];
  const push = (col: string, val: unknown) => {
    params.push(val);
    set.push(`${col} = $${params.length}`);
  };
  if (part.name !== undefined) push("name", part.name);
  if (part.articles !== undefined) push("articles", part.articles);
  if (part.weight_kg !== undefined) push("weight_kg", part.weight_kg);
  if (part.model !== undefined) push("model", part.model);
  if (part.description !== undefined) push("description", part.description);
  // product_type сознательно не трогаем (Smart trigger запрещает менять).
  if (set.length === 0) return;
  params.push(partId);
  const sql = `UPDATE smart.parts SET ${set.join(", ")} WHERE id = $${params.length} RETURNING id`;
  const { rows } = await client.query(sql, params);
  if (rows.length === 0) {
    throw new Error(`smart_id=${partId} disappeared during UPDATE (concurrent delete?)`);
  }
}

async function insertBrands(
  client: PoolClient,
  partId: string,
  brands: string[],
): Promise<void> {
  for (const b of brands) {
    await client.query(
      "INSERT INTO smart.part_brands (part_id, brand) VALUES ($1, $2)",
      [partId, b],
    );
  }
}

// Patch-merge для существующего компонента (is_draft=true): записываем поле в
// smart.parts только если оно там пусто. Заполненное не перезаписываем.
async function patchMergeComponent(
  client: PoolClient,
  partId: string,
  comp: SaveComponent,
): Promise<void> {
  const { rows } = await client.query<{
    name: string | null;
    articles: string[] | null;
    weight_kg: string | null;
    model: string | null;
    description: string | null;
  }>(
    "SELECT name, articles, weight_kg, model, description FROM smart.parts WHERE id = $1",
    [partId],
  );
  const cur = rows[0]!;
  const set: string[] = [];
  const params: unknown[] = [];
  const push = (col: string, val: unknown) => {
    params.push(val);
    set.push(`${col} = $${params.length}`);
  };
  if (comp.name !== undefined && isEmptyText(cur.name)) push("name", comp.name);
  if (comp.articles !== undefined && isEmptyArr(cur.articles)) push("articles", comp.articles);
  if (comp.weight_kg !== undefined && cur.weight_kg === null) push("weight_kg", comp.weight_kg);
  if (comp.model !== undefined && isEmptyText(cur.model)) push("model", comp.model);
  if (comp.description !== undefined && isEmptyText(cur.description))
    push("description", comp.description);
  if (set.length > 0) {
    params.push(partId);
    await client.query(
      `UPDATE smart.parts SET ${set.join(", ")} WHERE id = $${params.length}`,
      params,
    );
  }
  if (comp.brands !== undefined && comp.brands.length > 0) {
    const { rows: br } = await client.query<{ c: string }>(
      "SELECT COUNT(*)::text AS c FROM smart.part_brands WHERE part_id = $1",
      [partId],
    );
    if (parseInt(br[0]!.c, 10) === 0) {
      await insertBrands(client, partId, comp.brands);
    }
  }
}

function isEmptyText(v: string | null): boolean {
  return v === null || v === "";
}

function isEmptyArr(v: string[] | null): boolean {
  return v === null || v.length === 0;
}
