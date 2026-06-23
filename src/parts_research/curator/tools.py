"""@function_tool куратора (Этап 3). Все тулы живут в процессе куратора, без MCP.

- execute_sql — сырой SQL по parts_research (+ smart.* / brand_mapping.* через FDW), лог в agent_sql_log.
- save_to_smart — публикация в smart_test через FDW по save_to_smart.md (SAVEPOINT на каждый part).
- mark_needs_review — пометить run.
- web_search_exa / web_fetch_exa — прямой Exa БЕЗ кэша (кэш только в research-процессе).

Принцип «ошибки не скрываем»: исключение тула → текст ошибки модели; save_to_smart
даёт per-part error, а соседние parts всё равно сохраняются (SAVEPOINT)."""

from __future__ import annotations

import collections
import json
from dataclasses import dataclass
from decimal import Decimal

import asyncpg
import sqlparse
from exa_py import AsyncExa
from pydantic import BaseModel

from agents import RunContextWrapper, function_tool

from ..article_format import NOT_CANONICAL, OK, load_ruleset
from ..config import settings
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


# ── get_context ───────────────────────────────────────────────────────────────────
# Один тул вместо «модель сама сочиняет разведочный SQL»: по списку артикулов (или по
# голове очереди) отдаёт весь нужный для публикации контекст детали. Read-only.
def _gc_norm(articles: list[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for a in articles or []:
        n = (a or "").strip().upper()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


async def _gc_existing_prices(conn, smart_id: str) -> list[dict]:
    """Уже записанные офферы Smart-записи (по одному на магазин). Через write-FT
    market.observations_w (read-FT в parts_research нет); observed_at недоступен,
    поэтому отдаём distinct по магазину — достаточно, чтобы не плодить дубли цен."""
    rows = await conn.fetch(
        "SELECT DISTINCT s.name AS site, o.price, o.currency, o.url, o.note AS article "
        "FROM market.observations_w o JOIN market.sites s ON s.id = o.site_id WHERE o.smart_part_id = $1",
        smart_id,
    )
    return [{"site": r["site"], "price": float(r["price"]) if r["price"] is not None else None,
             "currency": r["currency"], "url": r["url"], "article": r["article"]} for r in rows]


GC_CHILD_DEPTH = 3  # на сколько уровней разворачивать состав smart-записей в smart_match


async def _gc_smart_detail(conn, smart_id: str, depth: int = GC_CHILD_DEPTH,
                           _seen: frozenset | None = None) -> dict | None:
    """Полная Smart-запись: поля + EN + бренды + цены + РЕКУРСИВНЫЙ состав (до depth уровней,
    защита от циклов). Дети разворачиваются так же полно, как родитель."""
    _seen = _seen or frozenset()
    if smart_id in _seen:
        return {"id": smart_id, "cycle": True}
    _seen = _seen | {smart_id}
    p = await conn.fetchrow(
        "SELECT id, name, articles, model, weight_kg, description, is_draft, is_unverified, vehicle_classes "
        "FROM smart.parts WHERE id = $1", smart_id)
    if p is None:
        return None
    en = await conn.fetchrow("SELECT name, description FROM smart.parts_en WHERE part_id = $1", smart_id)
    brands = [r["brand"] for r in await conn.fetch("SELECT brand FROM smart.part_brands WHERE part_id = $1", smart_id)]
    children = []
    if depth > 0:
        for c in await conn.fetch(
                "SELECT child_id, quantity FROM smart.part_components WHERE parent_id = $1 ORDER BY child_id", smart_id):
            cd = await _gc_smart_detail(conn, c["child_id"], depth - 1, _seen)
            if cd is not None:
                children.append({"quantity": c["quantity"], **cd})
    return {
        "id": p["id"], "name": p["name"], "articles": list(p["articles"] or []), "model": p["model"],
        "weight_kg": float(p["weight_kg"]) if p["weight_kg"] is not None else None,
        "description": p["description"], "is_draft": p["is_draft"], "is_unverified": p["is_unverified"],
        "vehicle_classes": list(p["vehicle_classes"] or []),
        "name_en": en["name"] if en else None, "description_en": en["description"] if en else None,
        "brands": brands,
        "components": children,
        "existing_prices": await _gc_existing_prices(conn, smart_id),
    }


async def _gc_smart_match(conn, articles: list[str]) -> list[dict]:
    """Smart-записи, пересекающиеся по любому из articles (полный контекст каждой, с детьми)."""
    if not articles:
        return []
    ids = [r["id"] for r in await conn.fetch(
        "SELECT id FROM smart.parts WHERE articles && $1::text[] ORDER BY id", list(articles))]
    res = []
    for sid in ids:
        d = await _gc_smart_detail(conn, sid)
        if d is not None:
            res.append(d)
    return res


async def _gc_confirmed(conn, run_id: int) -> set[str]:
    """confirmed-номера рана (4–20 симв.) — основа кластеризации дублей."""
    rows = await conn.fetch(
        "SELECT upper(a.article) AS art FROM draft_part_articles a "
        "JOIN draft_parts dp ON dp.id = a.draft_part_id "
        "WHERE dp.run_id = $1 AND a.confidence = 'confirmed' AND length(a.article) BETWEEN 4 AND 20", run_id)
    return {r["art"] for r in rows}


async def _gc_build_part(conn, ruleset, task_id: int, task_article: str, latest_run: int,
                         run_status: str, curator_note: str | None,
                         matched_inputs: list[dict]) -> dict | None:
    dp = await conn.fetchrow(
        "SELECT id, name, name_en, brand_oem, vehicle_classes, product_type, is_kit, weight_kg, "
        "weight_source_url, weight_evidence, models_text, models_source_urls, models_evidence, "
        "description, description_en, needs_review_reason FROM draft_parts WHERE run_id = $1", latest_run)
    if dp is None:
        return None
    arts = await conn.fetch(
        "SELECT article, confidence::text AS confidence, source_url, evidence, why_low_confidence, why_irrelevant "
        "FROM draft_part_articles WHERE draft_part_id = $1 ORDER BY id", dp["id"])
    latest_articles = {a["article"].upper() for a in arts}
    confirmed = [a["article"] for a in arts if a["confidence"] == "confirmed" and 4 <= len(a["article"]) <= 20]
    brands = set(dp["brand_oem"] or [])
    formats = []
    for a in confirmed:
        v = ruleset.validate(a, brands)
        formats.append({"article": a, "status": v.status, "expected_canonical": v.expected,
                        "rule_name": v.rule_name, "ok": v.status == OK})
    comps = await conn.fetch(
        "SELECT component_key, article, name, name_en, quantity, description, description_en, source_url, evidence "
        "FROM draft_kit_components WHERE draft_part_id = $1 ORDER BY id", dp["id"])
    comp_out = []
    for c in comps:
        cm = await _gc_smart_match(conn, [c["article"].upper()]) if c["article"] else []
        comp_out.append({**{k: c[k] for k in ("component_key", "article", "name", "name_en", "quantity",
                                              "description", "description_en", "source_url", "evidence")},
                         "smart_match": cm})
    pofk = await conn.fetch(
        "SELECT kit_article, kit_name, source_url, evidence FROM draft_part_of_kits WHERE draft_part_id = $1 ORDER BY id",
        dp["id"])
    prices = await conn.fetch(
        "SELECT article, site, price, currency, url, in_stock, evidence FROM draft_prices WHERE run_id = $1 ORDER BY id",
        latest_run)
    # already_published — на уровне задачи (публикация могла быть со старого рана).
    pub = await conn.fetchrow(
        "SELECT p.smart_id, p.published_at FROM publications p JOIN task_runs r ON r.id = p.run_id "
        "WHERE r.task_id = $1 ORDER BY p.id DESC LIMIT 1", task_id)
    smart_match = await _gc_smart_match(conn, [a.upper() for a in confirmed])
    return {
        "task_article": task_article, "run_id": latest_run, "run_status": run_status,
        "curator_note": curator_note,  # причина mark_needs_review (task_runs.error), если ран припаркован
        "matched_input_articles": [{**m, "in_latest_draft": m["article"] in latest_articles} for m in matched_inputs],
        "already_published": ({"published": True, "smart_id": pub["smart_id"], "published_at": pub["published_at"]}
                              if pub else {"published": False}),
        "draft": {
            "name": dp["name"], "name_en": dp["name_en"], "brand_oem": list(dp["brand_oem"] or []),
            "vehicle_classes": list(dp["vehicle_classes"] or []), "class_count": len(dp["vehicle_classes"] or []),
            "product_type": dp["product_type"], "is_kit": dp["is_kit"],
            "weight_kg": float(dp["weight_kg"]) if dp["weight_kg"] is not None else None,
            "weight_source_url": dp["weight_source_url"], "weight_evidence": dp["weight_evidence"],
            "models_text": dp["models_text"], "models_source_urls": list(dp["models_source_urls"] or []),
            "models_evidence": dp["models_evidence"], "description": dp["description"],
            "description_en": dp["description_en"], "needs_review_reason": dp["needs_review_reason"],
        },
        "articles": [dict(a) for a in arts],
        "confirmed_articles": confirmed,
        "article_formats": formats,
        "components": comp_out,
        "part_of_kits": [dict(p) for p in pofk],
        "prices": [dict(p) for p in prices],
        "smart_match": smart_match,
    }


class _GcUF:
    """Union-find для кластеризации ранов по пересечению confirmed-номеров."""

    def __init__(self):
        self._p: dict = {}

    def find(self, x):
        self._p.setdefault(x, x)
        while self._p[x] != x:
            self._p[x] = self._p[self._p[x]]
            x = self._p[x]
        return x

    def union(self, a, b):
        self._p[self.find(a)] = self.find(b)


def _gc_components(conf: dict[int, set]) -> dict[int, frozenset]:
    """run_id -> frozenset всех run_id его связной компоненты (транзитивно по общим confirmed)."""
    owner: dict[str, list] = collections.defaultdict(list)
    for rid, arts in conf.items():
        for a in arts:
            owner[a].append(rid)
    uf = _GcUF()
    for rid in conf:
        uf.find(rid)
    for owners in owner.values():
        for o in owners[1:]:
            uf.union(owners[0], o)
    comp: dict[int, set] = collections.defaultdict(set)
    for rid in conf:
        comp[uf.find(rid)].add(rid)
    return {rid: frozenset(comp[uf.find(rid)]) for rid in conf}


def _gc_bridges(members: list[int], conf: dict[int, set], pool: dict[int, dict],
                kit: dict[int, bool]) -> list[dict]:
    """Улики, почему раны попали в одну группу: по каждому члену — чем связан с остальными
    (общие confirmed-номера + сила пересечения) + name/is_kit. Чтобы агент мог отрезать
    ложный мостик или несовпадение «комплект vs голая деталь»."""
    out = []
    for m in members:
        links = []
        for o in members:
            if o == m:
                continue
            shared = sorted(conf[m] & conf[o])
            if shared:
                frac = len(shared) / max(1, min(len(conf[m]), len(conf[o])))
                links.append({"run_id": o, "shared_articles": shared, "overlap_frac": round(frac, 2)})
        out.append({"run_id": m, "task_article": pool[m]["article"], "is_kit": kit.get(m), "links": links})
    return out


async def _gc_candidate_pool(conn) -> tuple[dict[int, dict], dict[int, set]]:
    """Пул кандидатов очереди: последний done|needs_human_review ран на задачу, ещё не
    опубликованный. Возвращает (pool: run_id->{run_id,task_id,article,status,error},
    conf: run_id->set(confirmed-номеров)) для кластеризации."""
    rows = await conn.fetch(
        "SELECT DISTINCT ON (r.task_id) r.id AS run_id, r.task_id, t.article, r.status::text AS status, r.error "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.status IN ('done','needs_human_review') "
        "AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.run_id = r.id) "
        "ORDER BY r.task_id, r.id DESC")
    pool = {r["run_id"]: {"run_id": r["run_id"], "task_id": r["task_id"], "article": r["article"],
                          "status": r["status"], "error": r["error"]} for r in rows}
    rids = list(pool)
    conf: dict[int, set] = {r: set() for r in rids}
    if rids:
        for row in await conn.fetch(
                "SELECT dp.run_id AS rid, upper(a.article) AS art FROM draft_part_articles a "
                "JOIN draft_parts dp ON dp.id = a.draft_part_id "
                "WHERE dp.run_id = ANY($1::int[]) AND a.confidence = 'confirmed' "
                "AND length(a.article) BETWEEN 4 AND 20", rids):
            if row["rid"] in conf:
                conf[row["rid"]].add(row["art"])
    return pool, conf


@function_tool
async def get_context(wrapper: RunContextWrapper[CuratorRunContext],
                      articles: list[str] | None = None, limit: int | None = None) -> str:
    """Fetch publish-ready context in one call (read-only), GROUPED by likely duplicate.

    Two mutually-exclusive modes:
      * articles=[...] — resolve each number (matched against tasks.article AND any draft
        number of any confidence, so cross-/superseded numbers resolve). Per task the LATEST
        done OR needs_human_review run is returned (so parked items can be reviewed too), with
        run_status and curator_note (the mark_needs_review reason). Related tasks are pulled in
        via grouping (below) even if you didn't name their numbers.
      * limit=N — no articles: head of the queue (first N done & unpublished tasks, latest run
        each). Use for "process first N" / "обработай очередь".

    GROUPING: runs whose confirmed_articles overlap (transitively) land in one `group` — a
    HYPOTHESIS that they are the SAME physical part (one part sold under several numbers). A
    group with >1 member has `unverified=true`: VERIFY before merging. Use each member's
    name/is_kit and the `bridges` map (which shared numbers link members and how strongly,
    `overlap_frac`) to cut a false bridge — number overlap cannot tell a kit from a bare part
    or one propeller pitch from another, but names/is_kit can. Publish a verified group as ONE
    record covering all its numbers; do NOT split a real duplicate into two smart records.

    Per part: full draft card (name/EN, brands, vehicle_classes, weight, models, description/EN),
    every draft number with confidence/source/evidence, confirmed_articles, article_formats
    (canonical verdict — fix BEFORE save_to_smart rejects it), kit components (each with smart_match),
    part_of_kits, draft prices, already_published (task-level), run_status + needs_review_reason
    (research) + curator_note (parking reason), and smart_match: existing smart.parts overlapping
    this part's confirmed set, each with EN, brands, RECURSIVE composition and recorded prices —
    so you don't create duplicates but ENRICH the existing record.

    Cap: total parts returned ≤ settings.curator_get_context_max_parts. Groups are kept whole —
    a group that doesn't fit goes to `deferred` (request it next call); a single group larger
    than the cap goes to `oversized_groups` (rare — handle manually / raise the cap). Numbers with
    no research data go to no_research_data with status and any existing smart record.

    Args:
        articles: part numbers to fetch context for (mutually exclusive with limit).
        limit: take the first N unpublished done-runs from the queue head instead.
    """
    ctx = wrapper.context
    max_parts = settings.curator_get_context_max_parts
    log_id = await ctx.pool.fetchval(
        "INSERT INTO agent_sql_log (session_id, sql_text) VALUES ($1, $2) RETURNING id",
        ctx.session_id, f"-- get_context articles={articles!r} limit={limit!r}")
    norm = _gc_norm(articles)
    error: str | None = None
    out: dict = {}
    try:
        async with ctx.pool.acquire() as conn:
            ruleset = await load_ruleset(conn)
            pool, conf = await _gc_candidate_pool(conn)
            no_research: list[dict] = []

            if norm:
                # резолв затравок: tasks.article ИЛИ любой draft-номер -> задачи
                hit_tasks: dict[int, str] = {}
                matched_inputs_set: set[str] = set()
                for r in await conn.fetch(
                        "SELECT id AS tid, article FROM tasks WHERE upper(article) = ANY($1::text[])", norm):
                    hit_tasks[r["tid"]] = r["article"]
                    matched_inputs_set.add(r["article"].upper())
                for r in await conn.fetch(
                        "SELECT DISTINCT upper(a.article) AS input_art, t.id AS tid, t.article "
                        "FROM draft_part_articles a JOIN draft_parts dp ON dp.id = a.draft_part_id "
                        "JOIN task_runs rr ON rr.id = dp.run_id JOIN tasks t ON t.id = rr.task_id "
                        "WHERE upper(a.article) = ANY($1::text[])", norm):
                    hit_tasks[r["tid"]] = r["article"]
                    matched_inputs_set.add(r["input_art"])

                task_to_run = {pool[rid]["task_id"]: rid for rid in pool}
                seeds: list[int] = []
                for tid, art in hit_tasks.items():
                    run = await conn.fetchrow(
                        "SELECT id, status::text AS status, error FROM task_runs "
                        "WHERE task_id = $1 AND status IN ('done','needs_human_review') ORDER BY id DESC LIMIT 1", tid)
                    if run is None:
                        last = await conn.fetchrow(
                            "SELECT id, status::text AS status FROM task_runs WHERE task_id = $1 ORDER BY id DESC LIMIT 1", tid)
                        sm = await _gc_smart_match(conn, [art.upper()])
                        no_research.append({"article": art, "status": "no_done_or_nhr_run",
                                            "detail": f"latest run {last['id']} status={last['status']}" if last else "no runs",
                                            "smart": sm[0] if sm else None})
                        continue
                    rid = run["id"]
                    old = task_to_run.get(tid)  # держим один ран на задачу
                    if old is not None and old != rid:
                        pool.pop(old, None)
                        conf.pop(old, None)
                    pool[rid] = {"run_id": rid, "task_id": tid, "article": art,
                                 "status": run["status"], "error": run["error"]}
                    conf[rid] = await _gc_confirmed(conn, rid)
                    task_to_run[tid] = rid
                    seeds.append(rid)
                seeds = list(dict.fromkeys(seeds))
                for a in norm:
                    if a not in matched_inputs_set:
                        sm = await _gc_smart_match(conn, [a])
                        no_research.append({"article": a, "status": "in_smart_only" if sm else "not_found",
                                            "detail": "no research data" + ("" if sm else "; not in smart either"),
                                            "smart": sm[0] if sm else None})
                mode_head = {"mode": "articles", "requested": norm}
            else:
                n = max(1, min(int(limit) if limit is not None else max_parts, max_parts))
                seeds = sorted(rid for rid, m in pool.items() if m["status"] == "done")[:n]
                mode_head = {"mode": "queue", "limit": n}

            comp_of = _gc_components(conf)

            # добор целыми группами под потолок (компоненты дизъюнктны -> дедуп по frozenset)
            included: list[list[int]] = []
            deferred: list[dict] = []
            oversized: list[dict] = []
            used: set[int] = set()
            decided: set = set()
            for s in seeds:
                members = sorted(comp_of.get(s, frozenset({s})))
                key = frozenset(members)
                if key in decided:
                    continue
                decided.add(key)
                light = [{"run_id": m, "article": pool[m]["article"]} for m in members]
                if len(members) > max_parts:
                    oversized.append({"size": len(members), "members": light})
                elif len(used) + len(members) > max_parts:
                    deferred.append({"size": len(members), "members": light})
                else:
                    used.update(members)
                    included.append(members)

            groups = []
            for members in included:
                cards = []
                for m in sorted(members, reverse=True):  # свежий ран первым
                    matched_inputs = [{"article": a} for a in norm if a in conf.get(m, set())] if norm else []
                    card = await _gc_build_part(conn, ruleset, pool[m]["task_id"], pool[m]["article"],
                                                m, pool[m]["status"], pool[m]["error"], matched_inputs)
                    if card is None:
                        no_research.append({"article": pool[m]["article"], "status": "no_draft",
                                            "detail": f"run {m} has no draft", "smart": None})
                    else:
                        cards.append(card)
                if not cards:
                    continue
                kit = {c["run_id"]: c["draft"]["is_kit"] for c in cards}
                groups.append({
                    "size": len(cards),
                    "unverified": len(cards) > 1,
                    "parts": cards,
                    "bridges": _gc_bridges([c["run_id"] for c in cards], conf, pool, kit) if len(cards) > 1 else None,
                })

            out = {**mode_head, "returned_parts": sum(len(g["parts"]) for g in groups),
                   "groups": groups, "no_research_data": no_research}
            if deferred:
                out["deferred"] = deferred
            if oversized:
                out["oversized_groups"] = oversized
    except Exception as e:  # noqa: BLE001 — ошибку показываем, не глотаем
        error = f"{type(e).__name__}: {e}"
        out = {"error": error}
    await ctx.pool.execute(
        "UPDATE agent_sql_log SET rows_affected=$2, error=$3, finished_at=now() WHERE id=$1",
        log_id, out.get("returned_parts", 0), error)
    return json.dumps(out, ensure_ascii=False, default=str)


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
    # Прочие раны ТОЙ ЖЕ физической детали (дубли по другим номерам, сведённые в эту запись из
    # group `get_context`). На каждый пишется отдельная строка publications -> этот же smart_id,
    # чтобы они ушли из очереди. Раны разных деталей сюда класть нельзя.
    also_run_ids: list[int] | None = None
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
    # На вошедшие в запись раны (основной + сведённые дубли) — по строке publications -> один smart_id.
    pub_run_ids = list(dict.fromkeys([part.run_id, *(part.also_run_ids or [])]))
    await conn.executemany(
        "INSERT INTO publications (run_id, curator_session_id, smart_id) VALUES ($1, $2, $3)",
        [(rid, session_id, parent_id) for rid in pub_run_ids])
    return {"status": "ok", "smart_id": parent_id, "components": comp_results,
            "prices_recorded": prices_recorded, "published_run_ids": pub_run_ids}


async def _validate_part_formats(conn, part: SavePart, ruleset) -> list[str]:
    """Гейткипер smart: проверяет формат публикуемых артикулов по канон-правилам.
    Сами не правим. Проблемы пишем в article_format_problems (source='curator') в outer-
    транзакции — они переживут per-part SAVEPOINT-откат. Возвращает блокирующие артикулы:
    NOT_CANONICAL блокирует ВСЕГДА, NO_RULE — только в hard. Бренд берём из payload.brands,
    иначе из draft_parts.brand_oem; нет бренда -> не гейтим."""
    arts = list(part.articles or [])
    if not arts:
        return []
    brands = set(part.brands or [])
    if not brands:
        row = await conn.fetchrow("SELECT brand_oem FROM draft_parts WHERE run_id = $1", part.run_id)
        brands = set(row["brand_oem"] or []) if row else set()
    if not brands:
        return []
    hard = settings.format_validation_mode == "hard"
    blocking: list[str] = []
    for a in arts:
        v = ruleset.validate(a, brands)
        if v.status == OK:
            continue
        daid = await conn.fetchval(
            "SELECT dpa.id FROM draft_part_articles dpa JOIN draft_parts dp ON dp.id = dpa.draft_part_id "
            "WHERE dp.run_id = $1 AND dpa.article = $2 ORDER BY dpa.id LIMIT 1",
            part.run_id, a,
        )
        if daid is not None:
            await conn.execute(
                "INSERT INTO article_format_problems "
                "(draft_article_id, reason, expected_canonical, rule_name, source) "
                "VALUES ($1, $2, $3, $4, 'curator') "
                "ON CONFLICT (draft_article_id, source) DO UPDATE SET "
                "  reason = EXCLUDED.reason, expected_canonical = EXCLUDED.expected_canonical, "
                "  rule_name = EXCLUDED.rule_name, created_at = now()",
                daid, v.status, v.expected, v.rule_name,
            )
        if v.status == NOT_CANONICAL:
            blocking.append(f"{a} -> {v.expected}")
        elif hard:
            blocking.append(f"{a} (no rule)")
    return blocking


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
    ruleset = await load_ruleset(ctx.pool)
    async with ctx.pool.acquire() as conn:
        async with conn.transaction():
            for i, part in enumerate(parts):
                # Гейткипер формата ДО SAVEPOINT: проблемы пишем в outer-txn (переживут per-part откат).
                blocking = await _validate_part_formats(conn, part, ruleset)
                if blocking:
                    results.append({
                        "part_index": i, "status": "error",
                        "error": "article format not canonical (fix article to its canonical smart form): "
                                 + "; ".join(blocking),
                    })
                    continue
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
