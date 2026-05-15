// Запросы к Smart и brand_mapping через FDW — для формирования промпта.
import { pool } from "./pool.js";

export async function loadAllowedBrands(): Promise<string[]> {
  const { rows } = await pool.query<{ name: string }>(
    "SELECT name FROM smart.brands ORDER BY name",
  );
  return rows.map((r) => r.name);
}

export async function loadAllowedProductTypes(): Promise<string[]> {
  const { rows } = await pool.query<{ name: string }>(
    "SELECT name FROM smart.product_types ORDER BY name",
  );
  return rows.map((r) => r.name);
}

export async function loadBrandAliases(): Promise<Array<{ alias: string; canonical: string }>> {
  const { rows } = await pool.query<{ alias: string; canonical: string }>(
    "SELECT alias, canonical FROM brand_mapping.brand_aliases ORDER BY canonical, alias",
  );
  return rows;
}
