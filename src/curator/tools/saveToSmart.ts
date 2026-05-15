import type { PoolClient } from "pg";
import { pool } from "../../db/pool.js";
import { recordPublication } from "../../db/curator.js";

export type SaveOp = {
  table: "parts" | "part_brands" | "part_components";
  action: "insert";
  fields: Record<string, unknown>;
  run_id: number;
};

export type SaveOpResult =
  | { op_index: number; status: "ok"; smart_id?: string }
  | { op_index: number; status: "error"; error: string };

export async function saveToSmart(
  sessionId: number,
  operations: SaveOp[],
): Promise<SaveOpResult[]> {
  const results: SaveOpResult[] = [];
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    for (const [idx, op] of operations.entries()) {
      const sp = `op_${idx}`;
      await client.query(`SAVEPOINT ${sp}`);
      try {
        const smartId = await executeOp(client, sessionId, op);
        await client.query(`RELEASE SAVEPOINT ${sp}`);
        results.push({ op_index: idx, status: "ok", ...(smartId ? { smart_id: smartId } : {}) });
      } catch (err) {
        await client.query(`ROLLBACK TO SAVEPOINT ${sp}`);
        results.push({
          op_index: idx,
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

async function executeOp(client: PoolClient, sessionId: number, op: SaveOp): Promise<string> {
  if (op.table === "parts") {
    const f = op.fields;
    const { rows } = await client.query<{ id: string }>(
      `INSERT INTO smart.parts
         (name, articles, product_type, weight_kg, is_draft, description, model)
       VALUES ($1, $2, $3, $4, COALESCE($5, true), $6, $7)
       RETURNING id`,
      [
        toStr(f.name),
        toArr(f.articles),
        toStr(f.product_type),
        toNum(f.weight_kg),
        toBool(f.is_draft),
        toStr(f.description),
        toStr(f.model),
      ],
    );
    const smartId = rows[0]!.id;
    await recordPublication(client, op.run_id, sessionId, "parts", smartId, "insert");
    return smartId;
  }

  if (op.table === "part_brands") {
    const f = op.fields;
    const partId = mustStr(f.part_id, "part_brands.part_id");
    const brand = mustStr(f.brand, "part_brands.brand");
    await client.query(
      "INSERT INTO smart.part_brands (part_id, brand) VALUES ($1, $2)",
      [partId, brand],
    );
    await recordPublication(client, op.run_id, sessionId, "part_brands", partId, "insert");
    return partId;
  }

  if (op.table === "part_components") {
    const f = op.fields;
    const parentId = mustStr(f.parent_id, "part_components.parent_id");
    const childId = mustStr(f.child_id, "part_components.child_id");
    const quantity = toNum(f.quantity);
    const isUnverified = toBool(f.is_unverified);
    // can_be_sold_separately по правилам куратора всегда false (решение принимает человек позже).
    await client.query(
      `INSERT INTO smart.part_components
         (parent_id, child_id, quantity, can_be_sold_separately, is_unverified)
       VALUES ($1, $2, COALESCE($3, 1), false, COALESCE($4, true))`,
      [parentId, childId, quantity, isUnverified],
    );
    const compositeId = `${parentId}|${childId}`;
    await recordPublication(client, op.run_id, sessionId, "part_components", compositeId, "insert");
    return compositeId;
  }

  throw new Error(`unknown table: ${(op as { table: string }).table}`);
}

function toStr(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "string") return v;
  throw new Error(`expected string, got ${typeof v}`);
}
function mustStr(v: unknown, name: string): string {
  if (typeof v !== "string" || v.trim() === "") {
    throw new Error(`${name} must be a non-empty string`);
  }
  return v;
}
function toNum(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "number") return v;
  if (typeof v === "string" && v.trim() !== "" && !isNaN(Number(v))) return Number(v);
  throw new Error(`expected number, got ${typeof v}`);
}
function toBool(v: unknown): boolean | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "boolean") return v;
  throw new Error(`expected boolean, got ${typeof v}`);
}
function toArr(v: unknown): string[] | null {
  if (v === null || v === undefined) return null;
  if (Array.isArray(v) && v.every((x) => typeof x === "string")) return v as string[];
  throw new Error(`expected string[], got ${typeof v}`);
}
