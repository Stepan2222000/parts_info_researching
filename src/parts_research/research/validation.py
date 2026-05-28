"""Слой 3 (доменная пост-валидация с runtime-данными) + проверки backend'а:
валидация входного артикула и substring-check Exa-ответа.

Каждое правило живёт только в одном слое; здесь — правила, которым нужны
runtime-данные (allowed_brands/product_types, expected_part_number) и
backend-проверки, которых нет в Pydantic-схеме."""

from __future__ import annotations

import re

from .errors import NoExactDataError
from .schema import StructuredResult

ARTICLE_RE = re.compile(r"^[A-Z0-9\-]+$")


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
    allowed_product_types: list[str],
) -> None:
    """Доменные правила, требующие runtime-данных. Провал -> ValueError -> failed_validation."""
    if result.task_part_number != expected_part_number:
        raise ValueError(
            f"task_part_number {result.task_part_number!r} != expected {expected_part_number!r}"
        )

    bad_brands = [b for b in result.brand_oem if b not in allowed_brands]
    if bad_brands:
        raise ValueError(f"brand_oem not in allowed Smart brands: {bad_brands}")

    if result.product_type is not None and result.product_type not in allowed_product_types:
        raise ValueError(f"product_type {result.product_type!r} not in allowed set")

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
