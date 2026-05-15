// Smart-плагин: точное совпадение по артикулу + связанные компоненты.
import { pool } from "../db/pool.js";

export type SmartMatch = {
  smart_id: string;
  name: string;
  articles: string[];
  brands: string[];
  product_type: string;
  model: string | null;
  weight_kg: string | null;
  description: string | null;
  is_draft: boolean;
  components: Array<{ child_id: string; name: string; quantity: number; is_unverified: boolean }>;
  parents: Array<{ parent_id: string; name: string; quantity: number; is_unverified: boolean }>;
};

export type SmartPayload = { matches: SmartMatch[] };

export async function fetchSmartContext(article: string): Promise<SmartPayload> {
  const { rows: parts } = await pool.query<{
    id: string;
    name: string;
    articles: string[];
    product_type: string;
    model: string | null;
    weight_kg: string | null;
    description: string | null;
    is_draft: boolean;
  }>(
    `SELECT id, name, articles, product_type, model, weight_kg, description, is_draft
       FROM smart.parts
       WHERE $1 = ANY(articles)`,
    [article],
  );

  const matches: SmartMatch[] = [];
  for (const p of parts) {
    const [{ rows: brandRows }, { rows: comps }, { rows: parents }] = await Promise.all([
      pool.query<{ brand: string }>(
        "SELECT brand FROM smart.part_brands WHERE part_id = $1 ORDER BY brand",
        [p.id],
      ),
      pool.query<{ child_id: string; name: string; quantity: number; is_unverified: boolean }>(
        `SELECT pc.child_id, c.name, pc.quantity, pc.is_unverified
           FROM smart.part_components pc
           JOIN smart.parts c ON c.id = pc.child_id
           WHERE pc.parent_id = $1
           ORDER BY pc.child_id`,
        [p.id],
      ),
      pool.query<{ parent_id: string; name: string; quantity: number; is_unverified: boolean }>(
        `SELECT pc.parent_id, k.name, pc.quantity, pc.is_unverified
           FROM smart.part_components pc
           JOIN smart.parts k ON k.id = pc.parent_id
           WHERE pc.child_id = $1
           ORDER BY pc.parent_id`,
        [p.id],
      ),
    ]);

    matches.push({
      smart_id: p.id,
      name: p.name,
      articles: p.articles,
      brands: brandRows.map((b) => b.brand),
      product_type: p.product_type,
      model: p.model,
      weight_kg: p.weight_kg,
      description: p.description,
      is_draft: p.is_draft,
      components: comps,
      parents: parents,
    });
  }

  return { matches };
}

export function formatSmartContextForPrompt(payload: SmartPayload): string {
  if (payload.matches.length === 0) return "";
  const lines: string[] = [];
  lines.push("### Подсказка из Smart по этому артикулу (может быть устаревшей — проверь через Exa)");
  for (const m of payload.matches) {
    lines.push(`- smart_id: ${m.smart_id}, is_draft: ${m.is_draft}`);
    lines.push(`  name: ${m.name}`);
    lines.push(`  articles: ${m.articles.join(", ")}`);
    lines.push(`  brands: ${m.brands.join(", ") || "(нет)"}`);
    lines.push(`  product_type: ${m.product_type}`);
    if (m.model) lines.push(`  model: ${m.model}`);
    if (m.weight_kg !== null) lines.push(`  weight_kg: ${m.weight_kg}`);
    if (m.description) lines.push(`  description: ${m.description}`);
    if (m.components.length > 0) {
      lines.push("  components (children):");
      for (const c of m.components) {
        lines.push(`    - ${c.child_id} ×${c.quantity}: ${c.name}${c.is_unverified ? " [unverified]" : ""}`);
      }
    }
    if (m.parents.length > 0) {
      lines.push("  входит в наборы (parents):");
      for (const p of m.parents) {
        lines.push(`    - ${p.parent_id}: ${p.name}${p.is_unverified ? " [unverified]" : ""}`);
      }
    }
  }
  return lines.join("\n");
}
