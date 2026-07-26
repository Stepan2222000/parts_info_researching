"""Read-only выборки для UI (Этап 4): дашборд очереди и detail-панель run'а.

Всё через тот же asyncpg-пул к parts_research (+ smart.* через FDW). Ошибки не
скрываем — исключение летит в эндпоинт и отдаётся как HTTP-ошибка с текстом."""

from __future__ import annotations

import asyncpg

# Счётчики статусов по ПОСЛЕДНЕМУ run'у каждой задачи (re-run не двоит счётчик).
_COUNTS_SQL = """
SELECT status, count(*) AS n
FROM (
    SELECT DISTINCT ON (task_id) status
    FROM task_runs
    ORDER BY task_id, id DESC
) s
GROUP BY status
"""

# Карточки задач: последний run каждой задачи + краткие draft-поля + флаг публикации
# + профиль и текущий этап (для чипа прогресса у running-карточек).
# Отдаём ВСЕ задачи без лимита: UI держит полный список и ищет по нему на клиенте
# (поиск живёт в одном месте — на фронте). Очередь небольшая (десятки задач).
_CARDS_SQL = """
SELECT * FROM (
    SELECT DISTINCT ON (t.id)
        t.id            AS task_id,
        t.article,
        r.id            AS run_id,
        r.status::text  AS status,
        r.error,
        r.profile,
        r.created_at,
        r.started_at,
        r.finished_at,
        dp.name,
        dp.product_type,
        dp.vehicle_classes,
        dp.is_kit,
        dp.brand_oem,
        EXISTS (SELECT 1 FROM publications p WHERE p.run_id = r.id) AS published,
        (SELECT rt.stage FROM run_turns rt
         WHERE rt.run_id = r.id AND rt.status = 'running'
         ORDER BY rt.turn_idx DESC LIMIT 1) AS current_stage
    FROM tasks t
    JOIN task_runs r ON r.task_id = t.id
    LEFT JOIN draft_parts dp ON dp.run_id = r.id
    ORDER BY t.id, r.id DESC
) latest
ORDER BY run_id DESC
"""

# Бэклог куратора: задачи, чей ПОСЛЕДНИЙ run = done, но ещё не опубликован в Smart
# (нет строки в publications). Это «сколько куратор ещё не обработал».
_PENDING_PUBLICATION_SQL = """
SELECT count(*) AS n
FROM (
    SELECT DISTINCT ON (task_id) id, status
    FROM task_runs
    ORDER BY task_id, id DESC
) s
WHERE s.status = 'done'
  AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.run_id = s.id)
"""


async def load_queue(pool: asyncpg.Pool) -> dict:
    counts_rows, card_rows, pending_publication = await _gather(pool)
    return {
        "counts": {r["status"]: r["n"] for r in counts_rows},
        "cards": [_card(r) for r in card_rows],
        "pending_publication": pending_publication,
    }


async def _gather(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        counts_rows = await conn.fetch(_COUNTS_SQL)
        card_rows = await conn.fetch(_CARDS_SQL)
        pending_publication = await conn.fetchval(_PENDING_PUBLICATION_SQL)
    return counts_rows, card_rows, pending_publication


def _card(r: asyncpg.Record) -> dict:
    return {
        "task_id": r["task_id"],
        "article": r["article"],
        "run_id": r["run_id"],
        "status": r["status"],
        "error": r["error"],
        "profile": r["profile"],
        "current_stage": r["current_stage"],
        "created_at": _iso(r["created_at"]),
        "started_at": _iso(r["started_at"]),
        "finished_at": _iso(r["finished_at"]),
        "name": r["name"],
        "product_type": r["product_type"],
        "vehicle_classes": list(r["vehicle_classes"]) if r["vehicle_classes"] is not None else [],
        "is_kit": r["is_kit"],
        "brand_oem": list(r["brand_oem"]) if r["brand_oem"] is not None else [],
        "published": r["published"],
    }


async def load_run_detail(pool: asyncpg.Pool, run_id: int) -> dict | None:
    """Полная картина run'а для detail-панели. None — если run не найден."""
    async with pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT r.id AS run_id, r.task_id, r.status::text AS status, r.error, "
            "r.profile, r.stage_outcomes, "
            "r.result_json, r.created_at, r.started_at, r.finished_at, t.article "
            "FROM task_runs r JOIN tasks t ON t.id = r.task_id WHERE r.id = $1",
            run_id,
        )
        if run is None:
            return None

        draft = await conn.fetchrow("SELECT * FROM draft_parts WHERE run_id = $1", run_id)
        articles: list[asyncpg.Record] = []
        components: list[asyncpg.Record] = []
        part_of_kits: list[asyncpg.Record] = []
        nuances: list[asyncpg.Record] = []
        supersession: list[asyncpg.Record] = []
        if draft is not None:
            dpid = draft["id"]
            articles = await conn.fetch(
                "SELECT article, confidence::text AS confidence, source_url, evidence, "
                "why_low_confidence, why_irrelevant FROM draft_part_articles "
                "WHERE draft_part_id = $1 ORDER BY id",
                dpid,
            )
            nuances = await conn.fetch(
                "SELECT text, articles, source_url, evidence FROM draft_nuances "
                "WHERE draft_part_id = $1 ORDER BY id",
                dpid,
            )
            supersession = await conn.fetch(
                "SELECT newer, older, source_url, evidence FROM draft_supersession "
                "WHERE draft_part_id = $1 ORDER BY id",
                dpid,
            )
            components = await conn.fetch(
                "SELECT component_key, article, name, quantity, description, source_url, evidence, is_kit "
                "FROM draft_kit_components WHERE draft_part_id = $1 ORDER BY id",
                dpid,
            )
            part_of_kits = await conn.fetch(
                "SELECT kit_article, kit_name, source_url, evidence "
                "FROM draft_part_of_kits WHERE draft_part_id = $1 ORDER BY id",
                dpid,
            )

        publications = await conn.fetch(
            "SELECT id, smart_id, curator_session_id, published_by, published_at "
            "FROM publications WHERE run_id = $1 ORDER BY id",
            run_id,
        )

        prices = await conn.fetch(
            "SELECT site, price, currency, url, in_stock, article, evidence "
            "FROM draft_prices WHERE run_id = $1 ORDER BY price",
            run_id,
        )

    return {
        "run_id": run["run_id"],
        "task_id": run["task_id"],
        "article": run["article"],
        "status": run["status"],
        "error": run["error"],
        "profile": run["profile"],
        "stage_outcomes": run["stage_outcomes"],
        "result_json": run["result_json"],
        "created_at": _iso(run["created_at"]),
        "started_at": _iso(run["started_at"]),
        "finished_at": _iso(run["finished_at"]),
        "draft": _draft(draft) if draft is not None else None,
        "articles": [
            {
                "article": a["article"],
                "confidence": a["confidence"],
                "source_url": a["source_url"],
                "evidence": a["evidence"],
                "why_low_confidence": a["why_low_confidence"],
                "why_irrelevant": a["why_irrelevant"],
            }
            for a in articles
        ],
        "nuances": [
            {
                "text": n["text"],
                "articles": list(n["articles"]) if n["articles"] is not None else [],
                "source_url": n["source_url"],
                "evidence": n["evidence"],
            }
            for n in nuances
        ],
        "supersession": [
            {"newer": s["newer"], "older": s["older"], "source_url": s["source_url"], "evidence": s["evidence"]}
            for s in supersession
        ],
        "components": [
            {
                "component_key": c["component_key"],
                "article": c["article"],
                "name": c["name"],
                "is_kit": c["is_kit"],
                "quantity": c["quantity"],
                "description": c["description"],
                "source_url": c["source_url"],
                "evidence": c["evidence"],
            }
            for c in components
        ],
        "part_of_kits": [
            {
                "kit_article": p["kit_article"],
                "kit_name": p["kit_name"],
                "source_url": p["source_url"],
                "evidence": p["evidence"],
            }
            for p in part_of_kits
        ],
        "publications": [
            {
                "id": p["id"],
                "smart_id": p["smart_id"],
                "curator_session_id": p["curator_session_id"],
                "published_by": p["published_by"],
                "published_at": _iso(p["published_at"]),
            }
            for p in publications
        ],
        "prices": [
            {
                "site": p["site"],
                "price": float(p["price"]),
                "currency": p["currency"],
                "url": p["url"],
                "in_stock": p["in_stock"],
                "article": p["article"],
                "evidence": p["evidence"],
            }
            for p in prices
        ],
    }


def _draft(d: asyncpg.Record) -> dict:
    return {
        "name": d["name"],
        "name_en": d["name_en"],
        "brand_oem": list(d["brand_oem"]) if d["brand_oem"] is not None else [],
        "product_type": d["product_type"],
        "vehicle_classes": list(d["vehicle_classes"]) if d["vehicle_classes"] is not None else [],
        "description": d["description"],
        "description_en": d["description_en"],
        "is_kit": d["is_kit"],
        "weight_kg": float(d["weight_kg"]) if d["weight_kg"] is not None else None,
        "weight_source_url": d["weight_source_url"],
        "weight_evidence": d["weight_evidence"],
        "models_text": d["models_text"],
        "models_source_urls": list(d["models_source_urls"]) if d["models_source_urls"] is not None else [],
        "models_evidence": d["models_evidence"],
        "needs_review_reason": d["needs_review_reason"],
    }


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
