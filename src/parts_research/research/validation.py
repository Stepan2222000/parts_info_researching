"""Слой 3 (доменная пост-валидация с runtime-данными) + проверки backend'а:
валидация входного артикула и substring-check Exa-ответа.

Каждое правило живёт только в одном слое; здесь — правила, которым нужны
runtime-данные (allowed_brands/vehicle_classes, expected_part_number) и
backend-проверки, которых нет в Pydantic-схеме."""

from __future__ import annotations

import re

from .errors import NoExactDataError
from .schema import StructuredResult

ARTICLE_RE = re.compile(r"^[A-Z0-9\-]+$")

# Бюджет title фида "<арт1> / <арт2> <name>" — зеркалит curator/tools.py
# (TITLE_HARD_LIMIT/TITLE_SAFETY/TITLE_LIMIT). name/name_en обязаны влезать в title,
# иначе smart кидает RuntimeError на сборке фида, а save_to_smart откатывает part.
TITLE_HARD_LIMIT = 50
TITLE_SAFETY = 3
TITLE_LIMIT = TITLE_HARD_LIMIT - TITLE_SAFETY  # 47


def _feed_prefix(confirmed_articles: list[str]) -> str:
    """Префикс title по первым двум confirmed-артикулам (len 4-20) в порядке research.
    Зеркалит _feed_title в curator/tools.py: '<a1> / <a2> ' (или '<a1> ' / '')."""
    arts = [a for a in confirmed_articles if 4 <= len(a) <= 20][:2]
    if not arts:
        return ""
    return " / ".join(arts) + " "


def _validate_name_budget_and_en(result: StructuredResult) -> None:
    """name/name_en должны влезать в бюджет title, EN обязателен при наличии русского.
    Провал -> ValueError (на этапе записи draft JSON ран уходит в failed_validation).
    Бренд/модель в name держать НЕ нужно (они в brand_oem/model) — это и даёт длину."""
    # EN обязателен при наличии русского (и у детали, и у компонентов набора).
    if result.name is not None and result.name_en is None:
        raise ValueError("name_en is required when name is present (English goes to name_en, not into name parens)")
    if result.description is not None and result.description_en is None:
        raise ValueError("description_en is required when description is present")
    for comp in result.kit_contents:
        if comp.name_en is None:
            raise ValueError(f"kit component {comp.name!r}: name_en is required")
        if comp.description is not None and comp.description_en is None:
            raise ValueError(f"kit component {comp.name!r}: description_en is required when description is present")

    # Бюджет title (RU и EN) по первым двум confirmed-артикулам.
    confirmed = [a.article for a in result.numbers.article]
    prefix = _feed_prefix(confirmed)
    budget = TITLE_LIMIT - len(prefix)
    if result.name is not None and len(result.name) > budget:
        raise ValueError(
            f"name too long for feed title ({len(result.name)}>{budget}): {result.name!r}; "
            f"title prefix {prefix!r} eats {len(prefix)} of {TITLE_LIMIT} chars — "
            "keep name to the part type + distinguishing spec only (no brand, no model)"
        )
    if result.name_en is not None and len(result.name_en) > budget:
        raise ValueError(
            f"name_en too long for feed title ({len(result.name_en)}>{budget}): {result.name_en!r}"
        )


def pre_validate_article(raw: str) -> str:
    """Нормализация входного артикула + валидация регуляркой (как на входе в очередь)."""
    article = raw.strip().upper()
    if not ARTICLE_RE.match(article):
        raise ValueError(f"article {article!r} fails regex ^[A-Z0-9\\-]+$")
    return article


def substring_check(article: str, raw_json: str) -> None:
    """Точное вхождение артикула в Exa-ответ без OEM-нормализации (как в спеке).

    Дефисы/префиксы передаются как пришли — артикулы, по которым Exa
    нормализует представление иначе, могут уходить в failed_no_data.
    """
    if article.lower() not in raw_json.lower():
        raise NoExactDataError(f"article {article!r} not found in Exa results")


def post_validate(
    result: StructuredResult,
    *,
    expected_part_number: str,
    allowed_brands: list[str],
    allowed_vehicle_classes: list[str],
) -> None:
    """Доменные правила, требующие runtime-данных. Провал -> ValueError -> failed_validation."""
    if result.task_part_number != expected_part_number:
        raise ValueError(
            f"task_part_number {result.task_part_number!r} != expected {expected_part_number!r}"
        )

    bad_brands = [b for b in result.brand_oem if b not in allowed_brands]
    if bad_brands:
        raise ValueError(f"brand_oem not in allowed Smart brands: {bad_brands}")

    # Кросс-типовый мультикласс легален (Rotax: jetski+snowmobile) — проверяем
    # только что слаги из справочника. Дубли запрещены.
    bad_classes = [c for c in result.vehicle_classes if c not in allowed_vehicle_classes]
    if bad_classes:
        raise ValueError(f"vehicle_classes not in allowed set: {bad_classes}")
    if len(set(result.vehicle_classes)) != len(result.vehicle_classes):
        raise ValueError("vehicle_classes contains duplicates")

    if not result.is_kit and result.kit_contents:
        raise ValueError("is_kit=false but kit_contents is not empty")

    article_numbers = [a.article for a in result.numbers.article]
    if expected_part_number not in article_numbers:
        raise ValueError("task_part_number is absent from numbers.article")

    # Артикул не должен встречаться более чем в одном из массивов numbers.*
    buckets = {
        "article": {a.article for a in result.numbers.article},
        "article_low_confidence": {a.article for a in result.numbers.article_low_confidence},
        "irrelevant": {a.article for a in result.numbers.irrelevant},
    }
    seen: dict[str, str] = {}
    for bucket_name, arts in buckets.items():
        for art in arts:
            if art in seen:
                raise ValueError(f"article {art!r} appears in both {seen[art]} and {bucket_name}")
            seen[art] = bucket_name

    # Компонент набора не может быть самим артикулом задачи или его OEM-номером.
    article_set = set(article_numbers)
    for comp in result.kit_contents:
        if comp.article is None:
            continue
        if comp.article == expected_part_number or comp.article in article_set:
            raise ValueError(
                f"kit component {comp.article!r} duplicates the task article / numbers.article"
            )

    # Зеркально для связи «вверх»: родительский набор не может быть самим артикулом
    # задачи или его подтверждённым кроссом. Наборы с разным составом (в т.ч. когда
    # один входит в другой) — не кроссы; конфликт значит, что агент перепутал
    # взаимозаменяемость с вхождением в состав.
    for parent in result.part_of_kits:
        if parent.kit_article is None:
            continue
        if parent.kit_article == expected_part_number or parent.kit_article in article_set:
            raise ValueError(
                f"part_of_kits entry {parent.kit_article!r} duplicates the task article / "
                "numbers.article — a containing kit is not a cross of its component; "
                "keep it only in part_of_kits and out of numbers.article"
            )

    # name/name_en: EN обязателен, длина под бюджет title фида.
    _validate_name_budget_and_en(result)
