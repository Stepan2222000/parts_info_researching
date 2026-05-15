import type { PoolClient } from "pg";
import { pool } from "./pool.js";
import type { StructuredResult } from "../research/validation.js";

export async function saveDraftResult(runId: number, result: StructuredResult): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");

    const draftId = await insertDraftPart(client, runId, result);
    await insertArticles(client, draftId, result);
    await insertKitComponents(client, draftId, result);
    await insertPartOfKits(client, draftId, result);

    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }
}

async function insertDraftPart(
  client: PoolClient,
  runId: number,
  r: StructuredResult,
): Promise<number> {
  const { rows } = await client.query<{ id: number }>(
    `INSERT INTO draft_parts (
       run_id, name, brand_oem, description, is_kit,
       weight_kg, weight_source_url, weight_evidence,
       models_text, models_source_urls, models_evidence
     ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
     RETURNING id`,
    [
      runId,
      r.name,
      r.brand_oem,
      r.description,
      r.is_kit,
      r.weight?.kg ?? null,
      r.weight?.source_url ?? null,
      r.weight?.evidence ?? null,
      r.models?.text ?? null,
      r.models?.source_urls ?? null,
      r.models?.evidence ?? null,
    ],
  );
  return rows[0]!.id;
}

async function insertArticles(
  client: PoolClient,
  draftId: number,
  r: StructuredResult,
): Promise<void> {
  for (const a of r.numbers.article) {
    await client.query(
      `INSERT INTO draft_part_articles
         (draft_part_id, article, confidence, source_url, evidence)
       VALUES ($1,$2,'confirmed',$3,$4)`,
      [draftId, a.article, a.source_url, a.evidence],
    );
  }
  for (const a of r.numbers.article_low_confidence) {
    await client.query(
      `INSERT INTO draft_part_articles
         (draft_part_id, article, confidence, source_url, evidence, why_low_confidence)
       VALUES ($1,$2,'low_confidence',$3,$4,$5)`,
      [draftId, a.article, a.source_url, a.evidence, a.why_low_confidence],
    );
  }
  for (const a of r.numbers.irrelevant) {
    await client.query(
      `INSERT INTO draft_part_articles
         (draft_part_id, article, confidence, source_url, evidence, why_irrelevant)
       VALUES ($1,$2,'irrelevant',$3,$4,$5)`,
      [draftId, a.article, a.source_url, a.evidence, a.why_irrelevant],
    );
  }
}

async function insertKitComponents(
  client: PoolClient,
  draftId: number,
  r: StructuredResult,
): Promise<void> {
  for (const [key, c] of Object.entries(r.kit_contents)) {
    await client.query(
      `INSERT INTO draft_kit_components
         (draft_part_id, component_key, article, name, quantity, description, source_url, evidence)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
      [draftId, key, c.article, c.name, c.quantity, c.description, c.source_url, c.evidence],
    );
  }
}

async function insertPartOfKits(
  client: PoolClient,
  draftId: number,
  r: StructuredResult,
): Promise<void> {
  for (const k of r.part_of_kits) {
    await client.query(
      `INSERT INTO draft_part_of_kits
         (draft_part_id, kit_article, kit_name, source_url, evidence)
       VALUES ($1,$2,$3,$4,$5)`,
      [draftId, k.kit_article, k.kit_name, k.source_url, k.evidence],
    );
  }
}
