"""@function_tool куратора (Этап 3). Все тулы живут в процессе куратора, без MCP.

- execute_sql — сырой SQL по parts_research (+ smart.* / brand_mapping.* через FDW), лог в agent_sql_log.
- save_to_smart — публикация в smart_test через FDW по save_to_smart.md (SAVEPOINT на каждый part).
- mark_needs_review — пометить run.
- web_search_exa / web_fetch_exa — прямой Exa БЕЗ кэша (кэш только в research-процессе).

Принцип «ошибки не скрываем»: исключение тула → текст ошибки модели; save_to_smart
даёт per-part error, а соседние parts всё равно сохраняются (SAVEPOINT)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import asyncpg
import sqlparse
from exa_py import AsyncExa
from pydantic import BaseModel

from agents import RunContextWrapper, function_tool

from ..research.exa_client import _to_jsonable, pick_fetch, pick_search


@dataclass
class CuratorRunContext:
    pool: asyncpg.Pool
    exa: AsyncExa
    session_id: int


def _parse_count(tag: str) -> int:
    try:
        return int(tag.split()[-1])
    except (ValueError, IndexError):
        return 0


def _dec(v: float | None) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


# ── execute_sql ──────────────────────────────────────────────────────────────────
def _split_sql(sql: str) -> list[str]:
    """Режет скрипт на отдельные statement'ы через sqlparse (учитывает строки/кавычки/комментарии)."""
    return [s for s in (x.strip() for x in sqlparse.split(sql)) if s]


def _returns_rows(stmt: str) -> bool:
    head = stmt.lstrip().lower()
    return head.startswith("select") or head.startswith("with") or "returning" in head


@function_tool
async def execute_sql(wrapper: RunContextWrapper[CuratorRunContext], sql: str) -> str:
    """Execute raw SQL against parts_research (+ smart.* / brand_mapping.* via FDW).

    Поддерживает несколько statement'ов в одном вызове (разделяй `;`) — каждый выполняется
    по очереди на одном соединении. SELECT/WITH/RETURNING возвращают строки, прочее — row_count.
    Один statement → результат напрямую; несколько → {"statements": [...]}. Каждый вызов
    логируется в agent_sql_log. Ошибку statement'а показываем (не скрываем) и останавливаемся.

    Args:
        sql: один SQL-statement или скрипт из нескольких (через `;`).
    """
    ctx = wrapper.context
    log_id = await ctx.pool.fetchval(
        "INSERT INTO agent_sql_log (session_id, sql_text) VALUES ($1, $2) RETURNING id",
        ctx.session_id, sql,
    )
    statements = _split_sql(sql)
    results: list[dict] = []
    total = 0
    error: str | None = None
    try:
        async with ctx.pool.acquire() as conn:
            for i, st in enumerate(statements):
                try:
                    if _returns_rows(st):
                        rows = await conn.fetch(st)
                        results.append({"rows": [dict(r) for r in rows], "row_count": len(rows)})
                        total += len(rows)
                    else:
                        tag = await conn.execute(st)
                        n = _parse_count(tag)
                        results.append({"command": tag, "row_count": n})
                        total += n
                except Exception as e:  # noqa: BLE001 — ошибку statement'а показываем, не глотаем
                    error = f"statement {i + 1}/{len(statements)}: {type(e).__name__}: {e}"
                    results.append({"statement_index": i, "error": error})
                    break
    except Exception as e:  # noqa: BLE001 — ошибка соединения
        error = f"{type(e).__name__}: {e}"
    await ctx.pool.execute(
        "UPDATE agent_sql_log SET rows_affected=$2, error=$3, finished_at=now() WHERE id=$1",
        log_id, total, error,
    )
    # Один statement без ошибки — отдаём результат напрямую (как раньше); иначе массив.
    if len(statements) <= 1 and error is None:
        return json.dumps(results[0] if results else {"rows": [], "row_count": 0},
                          ensure_ascii=False, default=str)
    return json.dumps({"statements": results, "row_count": total, "error": error},
                      ensure_ascii=False, default=str)


# ── mark_needs_review ────────────────────────────────────────────────────────────
@function_tool
async def mark_needs_review(wrapper: RunContextWrapper[CuratorRunContext], run_id: int, reason: str) -> str:
    """Mark a task run as needs_human_review with a reason.

    Args:
        run_id: task_runs.id to mark.
        reason: short human-readable reason.
    """
    ctx = wrapper.context
    tag = await ctx.pool.execute(
        "UPDATE task_runs SET status='needs_human_review', error=$2, finished_at=now() WHERE id=$1",
        run_id, reason,
    )
    return json.dumps({"run_id": run_id, "updated": _parse_count(tag), "reason": reason}, ensure_ascii=False)


# ── Exa без кэша ──────────────────────────────────────────────────────────────────
@function_tool
async def web_search_exa(wrapper: RunContextWrapper[CuratorRunContext], query: str, num_results: int = 10) -> str:
    """Search the web via Exa (no cache). Returns JSON array of {url, title, highlights}.

    Args:
        query: search query.
        num_results: number of results (default 10).
    """
    resp = await wrapper.context.exa.search(query, num_results=num_results, contents={"highlights": True})
    return json.dumps(pick_search(_to_jsonable(resp)), ensure_ascii=False)


@function_tool
async def web_fetch_exa(wrapper: RunContextWrapper[CuratorRunContext], urls: list[str], max_characters: int = 3000) -> str:
    """Fetch full text of URLs via Exa (no cache). Returns JSON array of {url, title, text}.

    Args:
        urls: URLs to fetch.
        max_characters: max chars of text per page (default 3000).
    """
    resp = await wrapper.context.exa.get_contents(urls, text=True)
    return json.dumps(pick_fetch(_to_jsonable(resp), max_characters), ensure_ascii=False)


# ── save_to_smart ─────────────────────────────────────────────────────────────────
# Поля — strict + nullable: значение присутствует всегда, null == «не трогать»
# (как в save_to_smart.md). run_id обязателен (non-null).
class PriceOffer(BaseModel):
    """Один найденный оффер за оригинал на US-магазине. Пишется в
    parts_prices.market.record_price(smart_id, site, price, currency, url, note)."""
    site: str             # магазин (домен), ключ market.sites
    price: float
    currency: str | None  # null -> USD
    url: str | None
    article: str | None   # OEM-номер, по которому найдена цена (-> note observation)
    in_stock: bool | None
    evidence: str | None


class SaveComponent(BaseModel):
    smart_id: str | None
    name: str | None
    name_en: str | None
    articles: list[str] | None
    vehicle_classes: list[str] | None  # null/[] -> наследует классы родителя-кита
    weight_kg: float | None
    model: str | None
    description: str | None
    description_en: str | None
    brands: list[str] | None
    quantity: int | None


class SavePart(BaseModel):
    run_id: int
    smart_id: str | None
    name: str | None
    name_en: str | None
    articles: list[str] | None
    vehicle_classes: list[str] | None  # обязателен непустой при INSERT новой детали
    weight_kg: float | None
    model: str | None
    description: str | None
    description_en: str | None
    brands: list[str] | None
    components: list[SaveComponent] | None
    prices: list[PriceOffer] | None  # US-цены за оригинал; пишутся после публикации


async def _write_parts_en(conn, part_id: str, name_en, description_en) -> None:
    """EN-зеркало в smart.parts_en (patch-семантика: null == не трогать).
    parts_en.name NOT NULL — без name_en новую строку создать нельзя (пропускаем)."""
    if name_en is None and description_en is None:
        return
    exists = await conn.fetchval("SELECT 1 FROM smart.parts_en WHERE part_id=$1", part_id)
    if exists:
        fields = {"name": name_en, "description": description_en}
        upd = {k: v for k, v in fields.items() if v is not None}
        if upd:
            cols = list(upd)
            set_clause = ", ".join(f"{c}=${i + 2}" for i, c in enumerate(cols))
            await conn.execute(f"UPDATE smart.parts_en SET {set_clause} WHERE part_id=$1", part_id, *[upd[c] for c in cols])
    elif name_en is not None:
        await conn.execute(
            "INSERT INTO smart.parts_en (part_id, name, description) VALUES ($1, $2, $3)",
            part_id, name_en, description_en)


async def _insert_part(conn, *, name, name_en, vehicle_classes, articles, model, weight_kg, description, description_en) -> str:
    """INSERT новой smart.parts с явными is_draft=true, is_unverified=true. RETURNING smart_id.

    vehicle_classes — массив слагов: реестр part_vehicle_classes и проекцию
    product_type дальше синхронизирует сама smart (триггеры миграций 014-015).
    EN-зеркало (name_en/description_en) пишется в smart.parts_en тем же шагом.
    """
    part_id = await conn.fetchval(
        "INSERT INTO smart.parts (name, articles, vehicle_classes, model, weight_kg, description, is_draft, is_unverified) "
        "VALUES ($1, $2, $3, $4, $5, $6, true, true) RETURNING id",
        name, articles, vehicle_classes, model, _dec(weight_kg), description,
    )
    await _write_parts_en(conn, part_id, name_en, description_en)
    return part_id


async def _merge_vehicle_classes(conn, part_id: str, new_classes: list[str]) -> None:
    """Merge-only: добавляем недостающие классы, существующие НИКОГДА не удаляем
    (снятие класса — только человек; сверенные строки защищены freeze-триггером)."""
    current = await conn.fetchval("SELECT vehicle_classes FROM smart.parts WHERE id=$1", part_id) or []
    merged = list(current) + [c for c in new_classes if c not in current]
    if merged != list(current):
        await conn.execute("UPDATE smart.parts SET vehicle_classes=$2 WHERE id=$1", part_id, merged)


async def _set_brands(conn, part_id, brands) -> None:
    await conn.execute("DELETE FROM smart.part_brands WHERE part_id=$1", part_id)
    for b in brands:
        await conn.execute("INSERT INTO smart.part_brands (part_id, brand) VALUES ($1, $2)", part_id, b)


async def _update_nonempty_payload_fields(conn, part_id, part) -> None:
    """UPDATE существующего parent: пишем только непустые поля payload;
    vehicle_classes — merge-only (классы добавляются, не удаляются)."""
    fields = {
        "name": part.name, "articles": part.articles, "weight_kg": _dec(part.weight_kg),
        "model": part.model, "description": part.description,
    }
    upd = {k: v for k, v in fields.items() if v is not None}
    if upd:
        cols = list(upd)
        set_clause = ", ".join(f"{c}=${i + 2}" for i, c in enumerate(cols))
        await conn.execute(f"UPDATE smart.parts SET {set_clause} WHERE id=$1", part_id, *[upd[c] for c in cols])
    if part.vehicle_classes:
        await _merge_vehicle_classes(conn, part_id, part.vehicle_classes)
    await _write_parts_en(conn, part_id, part.name_en, part.description_en)


async def _save_component(conn, parent_id, comp: SaveComponent) -> dict:
    qty = comp.quantity if comp.quantity is not None else 1
    # классы компонента: свои из payload, иначе наследуем классы родителя-кита
    comp_classes = comp.vehicle_classes
    if not comp_classes:
        comp_classes = list(await conn.fetchval(
            "SELECT vehicle_classes FROM smart.parts WHERE id=$1", parent_id) or [])
    if comp.smart_id is None:
        cid = await _insert_part(conn, name=comp.name, name_en=comp.name_en, vehicle_classes=comp_classes,
                                 articles=comp.articles, model=comp.model, weight_kg=comp.weight_kg,
                                 description=comp.description, description_en=comp.description_en)
        if comp.brands:
            for b in comp.brands:
                await conn.execute("INSERT INTO smart.part_brands (part_id, brand) VALUES ($1, $2)", cid, b)
        await conn.execute(
            "INSERT INTO smart.part_components (parent_id, child_id, quantity, can_be_sold_separately) "
            "VALUES ($1, $2, $3, false)", parent_id, cid, qty)
        return {"status": "ok", "smart_id": cid, "linked": True}

    row = await conn.fetchrow("SELECT is_draft FROM smart.parts WHERE id=$1", comp.smart_id)
    if row is None:
        raise ValueError(f"component smart_id={comp.smart_id} not found")
    cid = comp.smart_id
    if row["is_draft"] is True:
        # patch-merge: пишем поле только если в Smart пусто
        cur = await conn.fetchrow(
            "SELECT name, articles, weight_kg, model, description FROM smart.parts WHERE id=$1", cid)
        upd: dict = {}
        if comp.name is not None and not cur["name"]:
            upd["name"] = comp.name
        if comp.description is not None and not cur["description"]:
            upd["description"] = comp.description
        if comp.model is not None and not cur["model"]:
            upd["model"] = comp.model
        if comp.weight_kg is not None and cur["weight_kg"] is None:
            upd["weight_kg"] = _dec(comp.weight_kg)
        if comp.articles is not None and not cur["articles"]:
            upd["articles"] = comp.articles
        if upd:
            cols = list(upd)
            set_clause = ", ".join(f"{c}=${i + 2}" for i, c in enumerate(cols))
            await conn.execute(f"UPDATE smart.parts SET {set_clause} WHERE id=$1", cid, *[upd[c] for c in cols])
        if comp.brands is not None:
            if await conn.fetchval("SELECT count(*) FROM smart.part_brands WHERE part_id=$1", cid) == 0:
                for b in comp.brands:
                    await conn.execute("INSERT INTO smart.part_brands (part_id, brand) VALUES ($1, $2)", cid, b)
        if comp_classes:
            await _merge_vehicle_classes(conn, cid, comp_classes)
        # EN компонента — fill-if-empty: создаём parts_en, только если строки ещё нет
        # (не перетираем уже выставленный человеком EN существующего компонента).
        if not await conn.fetchval("SELECT 1 FROM smart.parts_en WHERE part_id=$1", cid):
            await _write_parts_en(conn, cid, comp.name_en, comp.description_en)
    # is_draft=false → саму запись не трогаем, только создаём связку.
    await conn.execute(
        "INSERT INTO smart.part_components (parent_id, child_id, quantity, can_be_sold_separately) "
        "VALUES ($1, $2, $3, false)", parent_id, cid, qty)
    return {"status": "ok", "smart_id": cid, "linked": True}


async def _confirmed_article_order(conn, run_id: int) -> list[str]:
    """Эталонная цепочка артикулов из research для run'а: подтверждённые OEM-номера в
    порядке, заданном агентом (новые/актуальные → старые). draft_parts.run_id UNIQUE,
    порядок строк = порядок вставки (executemany) → ORDER BY id восстанавливает его."""
    rows = await conn.fetch(
        "SELECT a.article FROM draft_part_articles a "
        "JOIN draft_parts d ON d.id = a.draft_part_id "
        "WHERE d.run_id = $1 AND a.confidence = 'confirmed' ORDER BY a.id",
        run_id)
    return [r["article"] for r in rows]


def _order_articles(articles: list[str] | None, reference: list[str]) -> list[str] | None:
    """Переупорядочить articles под эталон reference (новые→старые из research), не меняя состав.
    Номера из reference идут в его порядке; добавленные куратором (которых нет в reference) —
    в хвост с сохранением их относительного порядка. None/пустой список — не трогаем."""
    if not articles:
        return articles
    idx = {a: i for i, a in enumerate(reference)}
    known = sorted((a for a in articles if a in idx), key=lambda a: idx[a])
    unknown = [a for a in articles if a not in idx]
    return known + unknown


async def _record_prices(conn, smart_id: str, prices: list[PriceOffer]) -> int:
    """Пишет офферы в parts_prices через FDW (market.*) В ТОЙ ЖЕ транзакции/SAVEPOINT,
    что и smart-публикация part'а: упали цены -> откатывается и публикация этого part'а
    (атомарно per-part). Логику market.record_price повторяем вручную (find-or-create
    site + insert observation), т.к. удалённую функцию через FDW не вызвать; пишем через
    write-FT без авто-колонок (id/created_at/observed_at генерит remote). Возвращает число офферов."""
    n = 0
    for off in prices:
        site = off.site.strip()
        site_id = await conn.fetchval("SELECT id FROM market.sites WHERE name = $1", site)
        if site_id is None:
            await conn.execute("INSERT INTO market.sites_w (name) VALUES ($1)", site)
            site_id = await conn.fetchval("SELECT id FROM market.sites WHERE name = $1", site)
        await conn.execute(
            "INSERT INTO market.observations_w (smart_part_id, site_id, price, currency, url, note, created_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, 'parts_research')",
            smart_id, site_id, _dec(off.price), (off.currency or "USD").upper(), off.url, off.article,
        )
        n += 1
    return n


# title фида = "<арт1> / <арт2> <name>" (articles newest-first, первые два),
# жёсткий лимит 50 симв. в smart (RuntimeError на сборке фида). Проверяем с запасом.
TITLE_HARD_LIMIT = 50
TITLE_SAFETY = 3
TITLE_LIMIT = TITLE_HARD_LIMIT - TITLE_SAFETY  # 47


def _feed_title(articles: list[str] | None, name: str | None) -> str:
    arts = list(articles or [])[:2]
    prefix = " / ".join(arts)
    name = (name or "").strip()
    return f"{prefix} {name}".strip() if prefix else name


async def _check_title_budget(conn, part_id: str) -> None:
    """Не публикуем part, чей title не влезет в фид. Считаем по РЕАЛЬНОМУ
    пост-состоянию: RU (smart.parts.name) и EN (smart.parts_en.name), формула
    '<арт1> / <арт2> <name>', лимит TITLE_LIMIT (50 минус запас). Превышение ->
    отказ part'а (SAVEPOINT откатится) с явной ошибкой — курятор укоротит name."""
    row = await conn.fetchrow("SELECT name, articles FROM smart.parts WHERE id=$1", part_id)
    arts = list(row["articles"] or [])
    ru = _feed_title(arts, row["name"])
    if len(ru) > TITLE_LIMIT:
        raise ValueError(
            f"feed title too long ({len(ru)}>{TITLE_LIMIT}): {ru!r} — укороти name в smart.parts "
            f"(лимит фида {TITLE_HARD_LIMIT}, запас {TITLE_SAFETY})")
    en_name = await conn.fetchval("SELECT name FROM smart.parts_en WHERE part_id=$1", part_id)
    if en_name is not None:
        en = _feed_title(arts, en_name)
        if len(en) > TITLE_LIMIT:
            raise ValueError(
                f"EN feed title too long ({len(en)}>{TITLE_LIMIT}): {en!r} — укороти name_en в smart.parts_en")


async def _save_one_part(conn, part: SavePart, session_id: int) -> dict:
    # Порядок articles задаёт research (новые→старые), а не куратор: берём эталон из
    # draft_part_articles этого run'а и переставляем под него. Состав не меняем.
    part.articles = _order_articles(part.articles, await _confirmed_article_order(conn, part.run_id))

    if part.smart_id is None:
        if not part.vehicle_classes:
            raise ValueError(
                "vehicle_classes is required (non-empty) when inserting a new part; "
                "determine the vehicle class before saving to smart")
        parent_id = await _insert_part(conn, name=part.name, name_en=part.name_en,
                                       vehicle_classes=part.vehicle_classes,
                                       articles=part.articles, model=part.model,
                                       weight_kg=part.weight_kg, description=part.description,
                                       description_en=part.description_en)
        if part.brands:
            await _set_brands(conn, parent_id, part.brands)
    else:
        row = await conn.fetchrow("SELECT is_draft, is_unverified FROM smart.parts WHERE id=$1", part.smart_id)
        if row is None:
            raise ValueError(f"smart_id={part.smart_id} not found")
        if row["is_draft"] is False:
            raise ValueError(f"smart_id={part.smart_id} is_draft=false; refusing update of published part")
        parent_id = part.smart_id
        await _update_nonempty_payload_fields(conn, parent_id, part)
        if part.brands is not None:
            await _set_brands(conn, parent_id, part.brands)
        if part.components is not None:
            if row["is_unverified"] is False:
                raise ValueError(
                    f"smart_id={part.smart_id} has is_unverified=false; composition is frozen, "
                    "remove `components` from payload or set is_unverified=true first via execute_sql")
            await conn.execute("DELETE FROM smart.part_components WHERE parent_id=$1", parent_id)

    comp_results = []
    if part.components is not None:
        for j, comp in enumerate(part.components):
            r = await _save_component(conn, parent_id, comp)
            r["index"] = j
            comp_results.append(r)

    await _check_title_budget(conn, parent_id)
    # Цены — в ТОЙ ЖЕ транзакции (через FDW parts_prices): атомарно с публикацией part'а.
    prices_recorded = await _record_prices(conn, parent_id, part.prices) if part.prices else 0
    await conn.execute(
        "INSERT INTO publications (run_id, curator_session_id, smart_id) VALUES ($1, $2, $3)",
        part.run_id, session_id, parent_id)
    return {"status": "ok", "smart_id": parent_id, "components": comp_results, "prices_recorded": prices_recorded}


@function_tool
async def save_to_smart(wrapper: RunContextWrapper[CuratorRunContext], parts: list[SavePart]) -> str:
    """Publish parts to smart_test (via FDW). Each part is its own SAVEPOINT — if one fails the
    others still persist. INSERT vs UPDATE is decided by presence of smart_id. One publications
    row is written per successful part. Full semantics: save_to_smart.md.

    Russian name/description go to smart.parts; English name_en/description_en go to smart.parts_en
    (no parts_en row is written without name_en — that column is NOT NULL). Optional `prices`
    (US offers for the original part) are written to parts_prices via FDW (market.*) WITHIN the
    same per-part transaction — atomic with the publish: if a price write fails, that part's whole
    publish (smart + prices) rolls back via its SAVEPOINT; neighbouring parts still persist.

    Args:
        parts: parts to publish (each a single part or a kit with components).
    """
    ctx = wrapper.context
    results: list[dict] = []
    async with ctx.pool.acquire() as conn:
        async with conn.transaction():
            for i, part in enumerate(parts):
                sp = conn.transaction()
                await sp.start()
                try:
                    res = await _save_one_part(conn, part, ctx.session_id)
                    await sp.commit()
                    res["part_index"] = i
                    results.append(res)
                except Exception as e:  # noqa: BLE001 — per-part откат через SAVEPOINT (smart + цены вместе), ошибку показываем
                    await sp.rollback()
                    results.append({"part_index": i, "status": "error", "error": f"{type(e).__name__}: {e}"})
    return json.dumps(results, ensure_ascii=False, default=str)
