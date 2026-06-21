"""Контекст промпта research-агента, загружаемый из БД через FDW на старте run'а:
список Smart-брендов, классы техники (vehicle_classes), алиасы брендов и
Smart-плагин (точное совпадение по артикулу + связанные компоненты).

Всё грузится параллельно (4 запроса). Smart-плагин — подсказка, не истина:
если ничего не найдено, payload = None и в промпт ничего не подмешивается."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import asyncpg

from ..article_format import load_ruleset

SMART_PLUGIN_NAME = "smart"


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
        # бренд запчасти на старте неизвестен (его определит модель) -> спека по всем брендам
        article_format_spec=ruleset.format_spec(),
    )
