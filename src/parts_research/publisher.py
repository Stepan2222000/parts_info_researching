"""Общая машинерия публикации в smart_test через FDW (семантика — save_to_smart.md).

Вынесена из curator/tools.py, чтобы публиковать могли ДВА пути с одинаковыми
правилами и защитами:
  - тул куратора save_to_smart (published_by='curator', с curator_session_id);
  - авто-режим воркера сразу после ресёрча (published_by='auto', без сессии) —
    см. auto_publish.py.

Каждый part — свой SAVEPOINT: упал один — соседние сохраняются. Порядок articles
выставляется по research + доказанным заменам difference-turn; нюансы уходят
фактами в part_knowledge, цены — в parts_prices, всё атомарно per-part.
Принцип «ошибки не скрываем»: ошибка part'а возвращается текстом в результате."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from .article_format import NOT_CANONICAL, OK, load_ruleset
from .config import settings


def _dec(v: float | None) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


# ── модели payload ────────────────────────────────────────────────────────────────
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


class PartOfLink(BaseModel):
    """Связь «вверх»: публикуемая запись — компонент родительского НАБОРА.
    Родитель задаётся либо smart_id (существующая запись), либо kit_article
    (find-or-create: реестр smart part_articles гарантирует <=1 кандидата;
    нет кандидата -> создаётся тонкий draft-родитель, тогда kit_name обязателен)."""
    smart_id: str | None
    kit_article: str | None
    kit_name: str | None
    quantity: int | None  # сколько таких деталей в наборе; null -> 1


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
    part_of: list[PartOfLink] | None  # наборы, в которые ВХОДИТ эта запись (связь вверх)
    prices: list[PriceOffer] | None  # US-цены за оригинал; пишутся после публикации


# ── запись полей smart ────────────────────────────────────────────────────────────
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


async def _link_part_of(conn, child_id: str, links: list[PartOfLink]) -> list[dict]:
    """Связи «вверх» для опубликованной записи child_id: find-or-create родителя-набора
    и INSERT smart.part_components(parent, child). Merge-only: существующие upward-связи
    НЕ удаляем (это состав ЧУЖИХ наборов, в отличие от overwrite собственных components);
    уже существующая связка — no-op (postgres_fdw не умеет ON CONFLICT — предпроверка).

    Поиск по kit_article отдаёт максимум одного кандидата: реестр smart part_articles
    держит PK по артикулу. Защиты самой smart не дублируем — self-link (CHECK
    no_self_reference), циклы (триггер check_components_cycle) и замороженный состав
    родителя (kit_freeze при is_unverified=false) отбиваются базой с внятным текстом;
    ошибка валит part (его SAVEPOINT откатывается), текст уходит модели."""
    out: list[dict] = []
    for link in links:
        created = False
        if link.smart_id is not None:
            parent_id = link.smart_id
            if await conn.fetchval("SELECT 1 FROM smart.parts WHERE id=$1", parent_id) is None:
                raise ValueError(f"part_of: smart_id={parent_id} not found")
        else:
            if not link.kit_article:
                raise ValueError("part_of: either smart_id or kit_article is required")
            cands = await conn.fetch(
                "SELECT id FROM smart.parts WHERE $1 = ANY(articles)", link.kit_article)
            if len(cands) > 1:  # при живом реестре part_articles невозможно; не угадываем
                raise ValueError(
                    f"part_of: article {link.kit_article!r} matches multiple smart parts "
                    f"{[r['id'] for r in cands]}; pass smart_id explicitly")
            if cands:
                parent_id = cands[0]["id"]
            else:
                if not link.kit_name:
                    raise ValueError(
                        f"part_of: kit {link.kit_article!r} is absent from smart and kit_name "
                        "is empty — provide kit_name to create the thin draft parent")
                # Тонкий draft-родитель: имя+артикул, классы наследуем от child;
                # дозаполнится собственным ресёрчем набора (smart_match найдёт по номеру).
                child_classes = list(await conn.fetchval(
                    "SELECT vehicle_classes FROM smart.parts WHERE id=$1", child_id) or [])
                parent_id = await _insert_part(
                    conn, name=link.kit_name, name_en=None, vehicle_classes=child_classes,
                    articles=[link.kit_article], model=None, weight_kg=None,
                    description=None, description_en=None)
                created = True
        qty = link.quantity if link.quantity is not None else 1
        exists = await conn.fetchval(
            "SELECT 1 FROM smart.part_components WHERE parent_id=$1 AND child_id=$2",
            parent_id, child_id)
        if exists:
            out.append({"kit_article": link.kit_article, "smart_id": parent_id,
                        "created_parent": created, "linked": "already"})
            continue
        await conn.execute(
            "INSERT INTO smart.part_components (parent_id, child_id, quantity, can_be_sold_separately) "
            "VALUES ($1, $2, $3, false)", parent_id, child_id, qty)
        out.append({"kit_article": link.kit_article, "smart_id": parent_id,
                    "created_parent": created, "linked": True})
    return out


# ── порядок articles (research + difference-turn) ─────────────────────────────────
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


def _apply_supersession_order(reference: list[str], edges: list[tuple[str, str]]) -> list[str]:
    """Уточнить research-порядок ДОКАЗАННЫМИ парами замен (draft_supersession,
    difference-turn): номера цепочки идут новейший→старейший, номера вне цепочки —
    после, в исходном research-порядке. Состав не меняется."""
    if not edges:
        return reference
    newer_of = {older: newer for newer, older in edges}   # older -> newer
    olders = set(newer_of)
    in_chain = olders | {newer for newer, _ in edges}
    chain: list[str] = []
    for top in (n for n in reference if n in in_chain and n not in olders):
        cur: str | None = top
        while cur is not None and cur not in chain:       # защита от цикла в данных
            chain.append(cur)
            nxt = [o for o, nw in newer_of.items() if nw == cur]
            cur = nxt[0] if nxt else None
    ordered = [n for n in chain if n in reference]
    return ordered + [n for n in reference if n not in ordered]


async def _supersession_edges(conn, run_id: int) -> list[tuple[str, str]]:
    """Пары замен (newer, older) из draft_supersession run'а (difference-turn,
    только confirmed-номера)."""
    rows = await conn.fetch(
        "SELECT s.newer, s.older FROM draft_supersession s "
        "JOIN draft_parts d ON d.id = s.draft_part_id WHERE d.run_id = $1 ORDER BY s.id",
        run_id)
    return [(r["newer"], r["older"]) for r in rows]


# ── факты-нюансы (part_knowledge) и цены (parts_prices) ───────────────────────────
def _fact_body(text: str, evidence: str, source_url: str) -> str:
    return f"{text}\n\nПруф: «{evidence}»\nИсточник: {source_url}"


async def _record_facts(conn, smart_id: str, main_run_id: int, pub_run_ids: list[int]) -> int:
    """Пишет нюансы (draft_nuances всех ранов записи) фактами в knowledge.knowledge_facts
    (part_knowledge, через FDW) В ТОЙ ЖЕ транзакции/SAVEPOINT, что и публикация part'а:
    упали факты -> откатывается и публикация (атомарно per-part).

    Маппинг: нюанс с articles -> строка на КАЖДЫЙ номер (scope_type='article');
    без articles -> одна строка на деталь (scope_type='part', scope_ref=smart_id).
    Одинаковые (scope, body) между ранами группы схлопываются. Нюансов нет -> базу
    знаний НЕ трогаем (страховка: пустота может значить упавший difference-turn).
    Перед вставкой гасим (is_active=false) прежние research-факты ЭТОЙ детали/её
    номеров — по принадлежности, не по ярлыку (пере-публикация новым run_id тоже
    гасит старое); ручные факты (source='manual' и пр.) не трогаем. Возвращает N."""
    nuances = await conn.fetch(
        "SELECT n.text, n.articles, n.source_url, n.evidence FROM draft_nuances n "
        "JOIN draft_parts d ON d.id = n.draft_part_id WHERE d.run_id = ANY($1::bigint[]) ORDER BY n.id",
        pub_run_ids)
    rows: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for n in nuances:
        body = _fact_body(n["text"], n["evidence"], n["source_url"])
        targets = [("article", a) for a in (n["articles"] or [])] or [("part", smart_id)]
        for scope_type, scope_ref in targets:
            key = (scope_type, scope_ref, body)
            if key not in seen:
                seen.add(key)
                rows.append(key)
    if not rows:
        return 0
    # Гасим старые авто-факты по принадлежности: сама запись + её номера (пост-состояние
    # smart) + номера, на которые ссылаются свежие факты (покрывает убранные из записи).
    published = list(await conn.fetchval("SELECT articles FROM smart.parts WHERE id=$1", smart_id) or [])
    fact_numbers = [ref for st, ref, _ in rows if st == "article"]
    numbers = list(dict.fromkeys(published + fact_numbers))
    await conn.execute(
        "UPDATE knowledge.knowledge_facts SET is_active = false "
        "WHERE source LIKE 'research:%' AND is_active AND ("
        "  (scope_type = 'part' AND scope_ref = $1) OR "
        "  (scope_type = 'article' AND scope_ref = ANY($2::text[])))",
        smart_id, numbers)
    source = f"research:difference_turn:{main_run_id}"
    await conn.executemany(
        "INSERT INTO knowledge.knowledge_facts (scope_type, scope_ref, body, source, is_active) "
        "VALUES ($1, $2, $3, $4, true)",
        [(st, ref, body, source) for st, ref, body in rows])
    return len(rows)


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


# ── title-бюджет фида ─────────────────────────────────────────────────────────────
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


# ── публикация одного part'а ──────────────────────────────────────────────────────
async def _save_one_part(conn, part: SavePart, *, session_id: int | None, published_by: str) -> dict:
    # Порядок articles задаёт research (новые→старые), а не куратор: берём эталон из
    # draft_part_articles этого run'а, уточняем ДОКАЗАННЫМИ парами замен difference-turn
    # (draft_supersession: новейший первым) и переставляем под него. Состав не меняем.
    reference = _apply_supersession_order(
        await _confirmed_article_order(conn, part.run_id),
        await _supersession_edges(conn, part.run_id))
    part.articles = _order_articles(part.articles, reference)

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

    # Связи «вверх» (эта запись — компонент других наборов): find-or-create родителя
    # и связка. В той же транзакции part'а; merge-only (см. _link_part_of).
    part_of_results = await _link_part_of(conn, parent_id, part.part_of) if part.part_of else []

    await _check_title_budget(conn, parent_id)
    # Цены — в ТОЙ ЖЕ транзакции (через FDW parts_prices): атомарно с публикацией part'а.
    prices_recorded = await _record_prices(conn, parent_id, part.prices) if part.prices else 0
    pub_run_ids = list(dict.fromkeys([part.run_id, *(part.also_run_ids or [])]))
    # Факты-нюансы — в part_knowledge (FDW), тоже атомарно с публикацией part'а.
    facts_recorded = await _record_facts(conn, parent_id, part.run_id, pub_run_ids)
    # На вошедшие в запись раны (основной + сведённые дубли) — по строке publications -> один smart_id.
    # published_by различает источник (curator/auto); curator_session_id у auto == NULL.
    await conn.executemany(
        "INSERT INTO publications (run_id, curator_session_id, smart_id, published_by) "
        "VALUES ($1, $2, $3, $4)",
        [(rid, session_id, parent_id, published_by) for rid in pub_run_ids])
    return {"status": "ok", "smart_id": parent_id, "components": comp_results,
            "part_of": part_of_results,
            "prices_recorded": prices_recorded, "facts_recorded": facts_recorded,
            "published_run_ids": pub_run_ids}


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


async def save_parts(conn, parts: list[SavePart], *, session_id: int | None, published_by: str) -> list[dict]:
    """Публикация пачки parts на переданном соединении. Общий вход обоих путей
    (тул куратора и авто-режим). Открывает транзакцию на conn (если вызвана уже
    внутри транзакции — станет вложенным SAVEPOINT'ом, семантика сохраняется);
    каждый part — свой SAVEPOINT внутри. Возвращает список результатов per part."""
    results: list[dict] = []
    ruleset = await load_ruleset(conn)
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
                res = await _save_one_part(conn, part, session_id=session_id, published_by=published_by)
                await sp.commit()
                res["part_index"] = i
                results.append(res)
            except Exception as e:  # noqa: BLE001 — per-part откат через SAVEPOINT (smart + цены вместе), ошибку показываем
                await sp.rollback()
                results.append({"part_index": i, "status": "error", "error": f"{type(e).__name__}: {e}"})
    return results
