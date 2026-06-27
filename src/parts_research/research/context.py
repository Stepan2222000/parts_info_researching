"""Контекст промпта research-агента, загружаемый из БД через FDW на старте run'а:
список Smart-брендов, классы техники (vehicle_classes), алиасы брендов,
Smart-плагин (точное совпадение по артикулу + связанные компоненты) и валидные
eBay-объявления по найденной детали.

Сначала грузится база параллельно (5 запросов). Smart-плагин — подсказка, не
истина: если ничего не найдено, payload = None и в промпт ничего не подмешивается.
eBay-блок зависит от smart_payload['id'] (это и есть validation_results.smart_id),
поэтому грузится после — только когда деталь в Smart нашлась."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import asyncpg

from ..article_format import load_ruleset

SMART_PLUGIN_NAME = "smart"

# eBay-подсказка: валидные (status='pass') объявления по детали из
# ebay_validation_item + ebay_data через FDW (миграция 010). Подсказка, не истина.
EBAY_PLUGIN_NAME = "ebay_validation"
# Кап описания одного объявления (символов). Specifics чистые и дешёвые — берём
# целиком; описания у части продавцов раздуты простынёй совместимости/навигацией
# магазина, поэтому режем. Замер: с капом 800 весь блок детали <= ~6.4k токенов.
EBAY_DESC_CAP = 800


@dataclass(frozen=True)
class VehicleClassInfo:
    slug: str
    title_ru: str
    product_type: str  # проекция на грубый тип (словарь smart.product_types)
    position: int


@dataclass(frozen=True)
class ResearchContext:
    allowed_brands: list[str]
    vehicle_classes: list[VehicleClassInfo]  # справочник классов техники (по position)
    brand_aliases: dict[str, str]  # alias -> canonical (Smart-бренд)
    smart_payload: dict[str, Any] | None  # подсказка Smart-плагина или None
    # валидные eBay-объявления по детали (подсказка); [] если детали нет в Smart
    # или у неё нет ни одного pass-объявления. Каждое: item_id/title/description/
    # specifics[{name,value}].
    ebay_listings: list[dict[str, Any]]
    article_format_spec: str  # спека канонических форматов артикулов (для промпта модели)

    @property
    def allowed_vehicle_classes(self) -> list[str]:
        return [vc.slug for vc in self.vehicle_classes]

    def derive_product_type(self, slugs: list[str]) -> str | None:
        """Деривация грубого типа для draft_parts: тип класса с минимальной position
        (та же логика, что во VIEW smart.parts_with_components)."""
        chosen = set(slugs)
        for vc in self.vehicle_classes:  # уже отсортированы по position
            if vc.slug in chosen:
                return vc.product_type
        return None


async def smart_plugin_lookup(pool: asyncpg.Pool, article: str) -> dict[str, Any] | None:
    """Точное совпадение по артикулу в Smart + компоненты (parents/children).

    Возвращает payload-подсказку или None, если в Smart ничего не найдено.
    """
    part = await pool.fetchrow(
        "SELECT id, name, articles, vehicle_classes, model, weight_kg, is_draft, description "
        "FROM smart.parts WHERE $1 = ANY(articles) LIMIT 1",
        article,
    )
    if part is None:
        return None

    part_id = part["id"]
    brands_rows, children_rows, parents_rows = await asyncio.gather(
        pool.fetch("SELECT brand FROM smart.part_brands WHERE part_id = $1", part_id),
        pool.fetch(
            "SELECT pc.child_id, p.name, pc.quantity "
            "FROM smart.part_components pc JOIN smart.parts p ON p.id = pc.child_id "
            "WHERE pc.parent_id = $1",
            part_id,
        ),
        pool.fetch(
            "SELECT pc.parent_id, p.name "
            "FROM smart.part_components pc JOIN smart.parts p ON p.id = pc.parent_id "
            "WHERE pc.child_id = $1",
            part_id,
        ),
    )

    return {
        "id": part_id,
        "name": part["name"],
        "articles": list(part["articles"] or []),
        "brands": [r["brand"] for r in brands_rows],
        "vehicle_classes": list(part["vehicle_classes"] or []),
        "model": part["model"],
        "weight_kg": float(part["weight_kg"]) if part["weight_kg"] is not None else None,
        "description": part["description"],
        "is_draft": part["is_draft"],
        "components": [
            {"child_id": r["child_id"], "name": r["name"], "quantity": r["quantity"]}
            for r in children_rows
        ],
        "part_of_kits": [
            {"parent_id": r["parent_id"], "name": r["name"]} for r in parents_rows
        ],
    }


async def ebay_listings_lookup(
    pool: asyncpg.Pool, smart_id: str
) -> list[dict[str, Any]]:
    """Валидные (status='pass') eBay-объявления детали через FDW (миграция 010).

    Strict by-part: берём объявления, прошедшие валидацию ИМЕННО для этой детали
    (smart_id), по всем её артикулам. INNER JOIN с ebay_data.items отбрасывает
    pass-строки, чьи item исчезли из ebay_data (рассинхрон evi). Описание режется
    до EBAY_DESC_CAP прямо в SQL — не тянем по сети простыни до 100k+ символов.

    Возвращает [] если у детали нет валидных объявлений — это легитимный исход,
    не ошибка. Реальные сбои FDW намеренно НЕ глушим: пусть падают (run -> failed),
    а не дают молча пустой контекст.
    """
    items = await pool.fetch(
        "SELECT i.item_id, i.title, left(b.content, $2) AS description "
        "FROM ebay_validation_item.validation_results vr "
        "JOIN ebay_data.items i ON i.item_id = vr.item_id "
        "LEFT JOIN ebay_data.blobs b ON b.hash = i.description_hash "
        "WHERE vr.status = 'pass' AND vr.smart_id = $1 "
        "ORDER BY i.item_id",
        smart_id, EBAY_DESC_CAP,
    )
    if not items:
        return []

    ids = [r["item_id"] for r in items]
    specs = await pool.fetch(
        "SELECT s.item_id, f.name, s.value FROM ebay_data.item_specifics s "
        "JOIN ebay_data.fields f ON f.field_id = s.field_id "
        "WHERE s.item_id = ANY($1::bigint[]) "
        "ORDER BY s.item_id, f.name",
        ids,
    )
    specs_by_item: dict[int, list[dict[str, str]]] = {}
    for r in specs:
        specs_by_item.setdefault(r["item_id"], []).append(
            {"name": r["name"], "value": r["value"]}
        )

    return [
        {
            "item_id": r["item_id"],
            "title": r["title"] or "",
            "description": r["description"] or "",
            "specifics": specs_by_item.get(r["item_id"], []),
        }
        for r in items
    ]


async def load_context(pool: asyncpg.Pool, article: str) -> ResearchContext:
    brands, classes, aliases, smart_payload, ruleset = await asyncio.gather(
        pool.fetch("SELECT name FROM smart.brands ORDER BY name"),
        pool.fetch(
            "SELECT slug, title_ru, product_type, position "
            "FROM smart.vehicle_classes ORDER BY position"
        ),
        pool.fetch("SELECT alias, canonical FROM brand_mapping.brand_aliases"),
        smart_plugin_lookup(pool, article),
        load_ruleset(pool),
    )
    # eBay-подсказка ключуется от smart_payload['id'] (= validation_results.smart_id):
    # нет smart-совпадения -> нет и eBay-блока (консистентно со smart_payload=None).
    ebay_listings = (
        await ebay_listings_lookup(pool, smart_payload["id"])
        if smart_payload is not None
        else []
    )
    return ResearchContext(
        allowed_brands=[r["name"] for r in brands],
        vehicle_classes=[
            VehicleClassInfo(
                slug=r["slug"], title_ru=r["title_ru"],
                product_type=r["product_type"], position=r["position"],
            )
            for r in classes
        ],
        brand_aliases={r["alias"]: r["canonical"] for r in aliases},
        smart_payload=smart_payload,
        ebay_listings=ebay_listings,
        # бренд запчасти на старте неизвестен (его определит модель) -> спека по всем брендам
        article_format_spec=ruleset.format_spec(),
    )
