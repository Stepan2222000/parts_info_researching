"""Авто-публикация: режим «сразу в smart после ресёрча, без куратора».

Включается флагом profile.auto_publish (submit по API/CLI; env-дефолт
RESEARCH_AUTO_PUBLISH). Воркер зовёт auto_publish_run из execute_run сразу после
того, как ран прошёл все валидации и признан done, но ДО выставления статуса —
потребитель, увидев терминальный статус, видит и исход публикации (нет окна
«done, а исход неизвестен»).

Публикуем только «зелёный коридор» — случаи, где решение куратора однозначно
восстановимо детерминированно. Всё остальное НЕ публикуем: ран остаётся done
в очереди куратора, а причина — в task_runs.auto_publish_outcome:
    {"decision": "published", "smart_id": ..., "prices_recorded": N, ...}
    {"decision": "skipped", "reason": "<почему отложено куратору>"}
    {"decision": "error", "error": "<текст ошибки публикации>"}

Гейты зелёного коридора (любой не прошёл -> skipped):
  - ран ещё не опубликован;
  - группа из одного рана: confirmed-номера не пересекаются ни с одним другим
    неопубликованным done/needs_human_review-раном (пересечение — гипотеза
    дубля из кластеризации get_context, разбирает куратор);
  - smart_match однозначен: по confirmed-номерам в smart ноль записей (INSERT)
    либо ровно одна (UPDATE/дообогащение — в т.ч. тонкие draft-родители,
    созданные part_of других ранов);
  - найденная запись is_draft=true; для набора её состав не заморожен
    (is_unverified=true);
  - ни один «лишний» номер существующей записи не помечен этим раном
    irrelevant (конфликт данных);
  - все бренды рана есть в smart.brands (FK; бренды сами не заводим);
  - title фида (RU и EN) влезает в лимит (иначе куратор укорачивает name).

part_of-связи «вверх» — best-effort: непубликуемая связка (родитель не найден и
нет kit_name / состав родителя заморожен / транзитивный дубль через под-набор)
отбрасывается с пометкой в outcome, НЕ валя публикацию детали.

После успешного INSERT (вне транзакции публикации) — воронка похожести
(similar.annotate_similar): шорт-лист + LLM-судья, гипотезы дублей в
dedup_candidates и в outcome.similar; публикацию не блокирует и не отменяет.

Публикация — общий publisher.save_parts (та же машинерия и защиты, что у тула
куратора), published_by='auto', curator_session_id NULL. Весь шаг сериализован
глобальным advisory-lock'ом (xact-scoped): параллельные воркеры не могут
одновременно опубликовать два рана одного семейства, поэтому гейт «группа из
одного» не гоняется сам с собой. Исход пишется в task_runs.auto_publish_outcome
В ТОЙ ЖЕ транзакции, что и публикация (атомарно). Ошибки не скрываем: любой сбой
-> decision=error с текстом, ран остаётся done в очереди куратора."""

from __future__ import annotations

import re

import asyncpg

from .publisher import (
    PartOfLink,
    PriceOffer,
    SaveComponent,
    SavePart,
    TITLE_LIMIT,
    _apply_supersession_order,
    _feed_title,
    _supersession_edges,
    save_parts,
)
from .similar import annotate_similar

# Своё пространство рядом с WORKER_LIVENESS_KEY=8273461 (db/tasks.py) — не пересекается.
AUTO_PUBLISH_LOCK_KEY = 8273462

# Ограничение smart на артикул (CHECK-regex в smart.parts): невалидные confirmed
# в payload не кладём (иначе INSERT упадёт целиком), отбрасываем с пометкой.
_SMART_ARTICLE_RE = re.compile(r"^[A-Z0-9\-]{4,20}$")

# Сколько уровней состава родителя обходим при проверке транзитивного дубля part_of.
_REACH_DEPTH = 5


class _Skip(Exception):
    """Гейт не прошёл: публикацию откладываем куратору с этой причиной."""


# Типизация причин skip для hard list (порядок важен: первый матч по подстроке).
# frozen_or_final разбирает ЧЕЛОВЕК (замки не снимаем автоматически), остальное — LLM-куратор.
_REASON_TYPES: list[tuple[str, str]] = [
    ("possible duplicate group", "group"),
    ("smart_match ambiguous", "ambiguous_smart_match"),
    ("matches multiple smart records", "ambiguous_smart_match"),
    ("is_draft=false", "frozen_or_final"),
    ("is_unverified=false", "frozen_or_final"),
    ("conflict: existing smart", "data_conflict"),
    ("brands not in smart.brands", "brand_missing"),
    ("feed title too long", "title_too_long"),
    ("EN feed title too long", "title_too_long"),
    ("smart format regex", "bad_article_format"),
    ("not canonical", "bad_article_format"),
    ("violates check constraint", "bad_article_format"),
    ("empty vehicle_classes", "no_classes"),
    ("no confirmed articles", "incomplete_draft"),
    ("draft has no name", "incomplete_draft"),
    ("no draft row", "incomplete_draft"),
    ("already published", "already_published"),
]


def classify_skip_reason(reason: str | None) -> str:
    """Причина _Skip (текст из auto_publish_outcome.reason) -> тип для hard list."""
    if not reason:
        return "other"
    for needle, kind in _REASON_TYPES:
        if needle in reason:
            return kind
    return "other"


async def auto_publish_run(pool: asyncpg.Pool, run_id: int, article: str) -> dict:
    """Пытается авто-опубликовать done-ран. Никогда не кидает исключение (research
    уже успешен — публикация его не валит); исход всегда записан в
    task_runs.auto_publish_outcome и возвращён."""
    try:
        outcome = await _attempt(pool, run_id)
    except Exception as e:  # noqa: BLE001 — сбой публикации не валит ран; текст наружу
        outcome = {"decision": "error", "error": f"{type(e).__name__}: {e}"}
        try:
            await pool.execute(
                "UPDATE task_runs SET auto_publish_outcome = $1 WHERE id = $2", outcome, run_id)
        except Exception as e2:  # noqa: BLE001 — даже запись исхода не удалась; отдаём оба текста
            outcome["outcome_write_error"] = f"{type(e2).__name__}: {e2}"
        return outcome
    if outcome.get("decision") == "published" and outcome.get("mode") == "insert":
        # Воронка похожести — ПОСЛЕ коммита публикации: LLM-вызов судьи не должен
        # держать глобальный advisory-lock. Сбой воронки публикацию не отменяет,
        # но виден текстом в outcome (ошибок не скрываем).
        try:
            outcome["similar"] = await annotate_similar(pool, run_id, outcome["smart_id"])
        except Exception as e:  # noqa: BLE001 — аннотация вспомогательная, текст наружу
            outcome["similar"] = {"error": f"{type(e).__name__}: {e}"}
        try:
            await pool.execute(
                "UPDATE task_runs SET auto_publish_outcome = $1 WHERE id = $2", outcome, run_id)
        except Exception as e2:  # noqa: BLE001
            outcome["outcome_write_error"] = f"{type(e2).__name__}: {e2}"
    return outcome


async def _attempt(pool: asyncpg.Pool, run_id: int) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Сериализация всех авто-публикаций: гейты и запись атомарны глобально.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", AUTO_PUBLISH_LOCK_KEY)
            try:
                part, notes = await _gates_and_payload(conn, run_id)
            except _Skip as s:
                outcome = {"decision": "skipped", "reason": str(s)}
                await _write_outcome(conn, run_id, outcome)
                return outcome
            results = await save_parts(conn, [part], session_id=None, published_by="auto")
            r = results[0]
            if r.get("status") != "ok":
                outcome = {"decision": "error", "error": r.get("error"), **notes}
            else:
                outcome = {
                    "decision": "published", "smart_id": r["smart_id"],
                    "mode": "update" if part.smart_id else "insert",
                    "prices_recorded": r["prices_recorded"],
                    "facts_recorded": r["facts_recorded"],
                    "components": len(r.get("components") or []),
                    "part_of": len(r.get("part_of") or []),
                    **notes,
                }
            await _write_outcome(conn, run_id, outcome)
            return outcome


async def _write_outcome(conn, run_id: int, outcome: dict) -> None:
    await conn.execute(
        "UPDATE task_runs SET auto_publish_outcome = $1 WHERE id = $2", outcome, run_id)


# ── гейты + сборка payload ────────────────────────────────────────────────────────
async def _gates_and_payload(conn, run_id: int) -> tuple[SavePart, dict]:
    """Проверяет зелёный коридор и детерминированно собирает SavePart из draft-таблиц
    рана (та же выводимость, которой пользуется куратор через get_context).
    Не прошло — _Skip с причиной."""
    run = await conn.fetchrow(
        "SELECT r.task_id, t.article FROM task_runs r JOIN tasks t ON t.id = r.task_id WHERE r.id = $1",
        run_id)
    if run is None:
        raise _Skip(f"run {run_id} not found")
    if await conn.fetchval("SELECT 1 FROM publications WHERE run_id = $1", run_id):
        raise _Skip("run already published")

    dp = await conn.fetchrow(
        "SELECT id, name, name_en, brand_oem, vehicle_classes, is_kit, weight_kg, "
        "models_text, description, description_en FROM draft_parts WHERE run_id = $1", run_id)
    if dp is None:
        raise _Skip("no draft row for run")
    if not dp["name"]:
        raise _Skip("draft has no name")
    if not (dp["vehicle_classes"] or []):
        raise _Skip("draft has empty vehicle_classes")

    arts = await conn.fetch(
        "SELECT article, confidence::text AS confidence FROM draft_part_articles "
        "WHERE draft_part_id = $1 ORDER BY id", dp["id"])
    confirmed = [a["article"] for a in arts if a["confidence"] == "confirmed"]
    irrelevant = {a["article"].upper() for a in arts if a["confidence"] == "irrelevant"}
    if not confirmed:
        raise _Skip("no confirmed articles")

    # Гейт «группа из одного»: пересечение confirmed с любым другим неопубликованным
    # done/nhr-раном (голова кластеризации get_context) — гипотеза дубля, куратору.
    confirmed_upper = list(dict.fromkeys(a.upper() for a in confirmed))
    cluster_keys = [a for a in confirmed_upper if 4 <= len(a) <= 20]
    overlap = await conn.fetch(
        "WITH pool AS ("
        "  SELECT DISTINCT ON (r.task_id) r.id AS run_id, t.article"
        "  FROM task_runs r JOIN tasks t ON t.id = r.task_id"
        "  WHERE r.status IN ('done','needs_human_review') AND r.task_id <> $2"
        "    AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.run_id = r.id)"
        "  ORDER BY r.task_id, r.id DESC)"
        "SELECT DISTINCT pool.run_id, pool.article FROM pool "
        "JOIN draft_parts dp ON dp.run_id = pool.run_id "
        "JOIN draft_part_articles a ON a.draft_part_id = dp.id "
        "WHERE a.confidence = 'confirmed' AND length(a.article) BETWEEN 4 AND 20 "
        "  AND upper(a.article) = ANY($1::text[]) LIMIT 10",
        cluster_keys, run["task_id"])
    if overlap:
        peers = ", ".join(f"run {r['run_id']} ({r['article']})" for r in overlap)
        raise _Skip(f"possible duplicate group: confirmed numbers overlap unpublished {peers}")

    # Гейт smart_match: 0 записей -> INSERT; ровно 1 draft-запись -> UPDATE; иначе куратору.
    matches = await conn.fetch(
        "SELECT id, is_draft, is_unverified, articles FROM smart.parts "
        "WHERE ARRAY(SELECT upper(x) FROM unnest(articles) x) && $1::text[]",
        confirmed_upper)
    smart_id: str | None = None
    extras: list[str] = []
    if len(matches) > 1:
        ids = [m["id"] for m in matches]
        raise _Skip(f"smart_match ambiguous: confirmed numbers overlap {len(matches)} smart records {ids}")
    if matches:
        m = matches[0]
        if m["is_draft"] is False:
            raise _Skip(f"smart_match {m['id']} is_draft=false (finalized record)")
        if dp["is_kit"] and m["is_unverified"] is False:
            raise _Skip(f"smart_match {m['id']} is_unverified=false (composition frozen, run is a kit)")
        confirmed_set = set(confirmed_upper)
        extras = [a for a in (m["articles"] or []) if a.upper() not in confirmed_set]
        conflict = [a for a in extras if a.upper() in irrelevant]
        if conflict:
            raise _Skip(
                f"conflict: existing smart {m['id']} numbers {conflict} are marked irrelevant by this run")
        smart_id = m["id"]

    # Гейт брендов: FK на smart.brands; бренды сами не заводим.
    brands = list(dp["brand_oem"] or [])
    smart_brands = {r["name"] for r in await conn.fetch("SELECT name FROM smart.brands")}
    missing = [b for b in brands if b not in smart_brands]
    if missing:
        raise _Skip(f"brands not in smart.brands: {missing}")

    notes: dict = {}
    # Порядок articles — как выставит publisher: research-порядок confirmed, уточнённый
    # supersession; «лишние» номера существующей записи — в хвост (union, состав не режем).
    reference = _apply_supersession_order(confirmed, await _supersession_edges(conn, run_id))
    valid_articles = [a for a in reference if _SMART_ARTICLE_RE.fullmatch(a.upper())]
    dropped_articles = [a for a in reference if a not in valid_articles]
    if dropped_articles:
        notes["articles_dropped_bad_format"] = dropped_articles
    if not valid_articles:
        raise _Skip(f"no confirmed articles pass smart format regex: {reference}")
    final_articles = valid_articles + extras

    # Гейт title-бюджета (пред-проверка по будущему пост-состоянию; publisher проверит ещё раз).
    ru_title = _feed_title(final_articles, dp["name"])
    if len(ru_title) > TITLE_LIMIT:
        raise _Skip(f"feed title too long ({len(ru_title)}>{TITLE_LIMIT}): {ru_title!r}")
    if dp["name_en"]:
        en_title = _feed_title(final_articles, dp["name_en"])
        if len(en_title) > TITLE_LIMIT:
            raise _Skip(f"EN feed title too long ({len(en_title)}>{TITLE_LIMIT}): {en_title!r}")

    components = await _build_components(conn, dp)
    part_of, dropped_links = await _build_part_of(conn, dp["id"], smart_id, run["article"])
    if dropped_links:
        notes["part_of_dropped"] = dropped_links
    prices, skipped_sites = await _build_prices(conn, run_id, smart_id)
    if skipped_sites:
        notes["prices_skipped_existing_sites"] = skipped_sites

    part = SavePart(
        run_id=run_id, also_run_ids=None, smart_id=smart_id,
        name=dp["name"], name_en=dp["name_en"],
        articles=final_articles,
        vehicle_classes=list(dp["vehicle_classes"] or []),
        weight_kg=float(dp["weight_kg"]) if dp["weight_kg"] is not None else None,
        model=dp["models_text"],
        description=dp["description"], description_en=dp["description_en"],
        brands=brands or None,
        components=components, part_of=part_of, prices=prices,
    )
    return part, notes


async def _smart_by_article(conn, article: str) -> list[asyncpg.Record]:
    """Записи smart, содержащие номер (регистронезависимо). Реестр part_articles
    гарантирует <=1; больше — грязные данные, наверх решает вызывающий."""
    return await conn.fetch(
        "SELECT id, is_draft, is_unverified FROM smart.parts "
        "WHERE upper($1) = ANY(ARRAY(SELECT upper(x) FROM unnest(articles) x))",
        article)


async def _build_components(conn, dp) -> list[SaveComponent] | None:
    """Компоненты payload из draft_kit_components. Существующий в smart компонент
    линкуется по smart_id (patch-merge полей делает publisher), отсутствующий —
    создаётся. Бренды компонент наследует от brand_oem рана (OEM-состав набора);
    классы наследуются от родителя внутри publisher. Пустой состав -> None
    (состав существующей записи не трогаем)."""
    comps = await conn.fetch(
        "SELECT article, name, name_en, quantity, description, description_en "
        "FROM draft_kit_components WHERE draft_part_id = $1 ORDER BY id", dp["id"])
    if not comps:
        return None
    parent_brands = list(dp["brand_oem"] or []) or None
    out: list[SaveComponent] = []
    for c in comps:
        cid: str | None = None
        if c["article"]:
            found = await _smart_by_article(conn, c["article"])
            if len(found) > 1:
                raise _Skip(
                    f"component article {c['article']!r} matches multiple smart records "
                    f"{[f['id'] for f in found]}")
            if found:
                cid = found[0]["id"]
        out.append(SaveComponent(
            smart_id=cid, name=c["name"], name_en=c["name_en"],
            articles=[c["article"]] if c["article"] else None,
            vehicle_classes=None,  # наследует классы родителя-кита (publisher)
            weight_kg=None, model=None,
            description=c["description"], description_en=c["description_en"],
            brands=parent_brands, quantity=c["quantity"],
        ))
    return out


async def _direct_children(conn, parent_id: str) -> set[str]:
    return {r["child_id"] for r in await conn.fetch(
        "SELECT child_id FROM smart.part_components WHERE parent_id = $1", parent_id)}


async def _reachable_ids(conn, root_id: str, max_depth: int = _REACH_DEPTH) -> set[str]:
    """Все записи, достижимые вниз по составу root (BFS, цикло-безопасно)."""
    seen: set[str] = set()
    frontier = [root_id]
    for _ in range(max_depth):
        if not frontier:
            break
        rows = await conn.fetch(
            "SELECT DISTINCT child_id FROM smart.part_components WHERE parent_id = ANY($1::text[])",
            frontier)
        frontier = [r["child_id"] for r in rows if r["child_id"] not in seen and r["child_id"] != root_id]
        seen.update(frontier)
    return seen


async def _build_part_of(conn, draft_part_id: int, smart_id: str | None,
                         run_article: str) -> tuple[list[PartOfLink] | None, list[dict]]:
    """Связи «вверх» из draft_part_of_kits. Best-effort: связка, которую нельзя
    безопасно создать детерминированно, отбрасывается с причиной (в outcome), не
    валя публикацию. Правила отбрасывания: родитель==сама запись; связь уже
    достижима транзитивно через под-набор; состав родителя заморожен; родителя
    нет в smart и нет kit_name для тонкого draft-родителя."""
    links = await conn.fetch(
        "SELECT kit_article, kit_name FROM draft_part_of_kits WHERE draft_part_id = $1 ORDER BY id",
        draft_part_id)
    if not links:
        return None, []
    out: list[PartOfLink] = []
    dropped: list[dict] = []

    def drop(link, reason: str) -> None:
        dropped.append({"kit_article": link["kit_article"], "reason": reason})

    for link in links:
        if not link["kit_article"]:
            drop(link, "empty kit_article")
            continue
        if link["kit_article"].upper() == run_article.upper():
            drop(link, "kit_article equals the run's own article (self-link)")
            continue
        found = await _smart_by_article(conn, link["kit_article"])
        if len(found) > 1:
            drop(link, f"kit_article matches multiple smart records {[f['id'] for f in found]}")
            continue
        if found:
            parent = found[0]
            if smart_id is not None and parent["id"] == smart_id:
                drop(link, "parent is the published record itself (self-link)")
                continue
            if smart_id is not None:
                direct = await _direct_children(conn, parent["id"])
                if smart_id not in direct:
                    if parent["is_unverified"] is False:
                        drop(link, f"parent {parent['id']} composition frozen (is_unverified=false)")
                        continue
                    reach = await _reachable_ids(conn, parent["id"])
                    if smart_id in reach:
                        drop(link, f"transitive duplicate: already reachable from {parent['id']} via sub-kit")
                        continue
            elif parent["is_unverified"] is False:
                # Новая запись не может войти в замороженный состав.
                drop(link, f"parent {parent['id']} composition frozen (is_unverified=false)")
                continue
            out.append(PartOfLink(smart_id=parent["id"], kit_article=link["kit_article"],
                                  kit_name=link["kit_name"], quantity=None))
        else:
            if not link["kit_name"]:
                drop(link, "kit absent from smart and kit_name is empty (cannot create thin parent)")
                continue
            out.append(PartOfLink(smart_id=None, kit_article=link["kit_article"],
                                  kit_name=link["kit_name"], quantity=None))
    return (out or None), dropped


async def _build_prices(conn, run_id: int, smart_id: str | None) -> tuple[list[PriceOffer] | None, list[str]]:
    """Офферы payload из draft_prices. Для UPDATE существующей записи не плодим дубли
    наблюдений: сайты, по которым у записи уже есть офферы, пропускаем (как куратор
    по existing_prices из get_context)."""
    rows = await conn.fetch(
        "SELECT article, site, price, currency, url, in_stock, evidence "
        "FROM draft_prices WHERE run_id = $1 ORDER BY id", run_id)
    if not rows:
        return None, []
    existing_sites: set[str] = set()
    if smart_id is not None:
        existing_sites = {r["name"] for r in await conn.fetch(
            "SELECT DISTINCT s.name FROM market.observations_w o "
            "JOIN market.sites s ON s.id = o.site_id WHERE o.smart_part_id = $1", smart_id)}
    out: list[PriceOffer] = []
    skipped: list[str] = []
    for r in rows:
        site = (r["site"] or "").strip()
        if site in existing_sites:
            if site not in skipped:
                skipped.append(site)
            continue
        out.append(PriceOffer(
            site=site, price=float(r["price"]), currency=r["currency"], url=r["url"],
            article=r["article"], in_stock=r["in_stock"], evidence=r["evidence"]))
    return (out or None), skipped
