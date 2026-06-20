"""Pydantic-контракт финального JSON (StructuredResult) — слои 1-2 валидации.

Слой 1 — strict JSON schema: структуру/типы/required/additionalProperties=false
         Agents SDK строит автоматически из output_type=StructuredResult и шлёт
         эндпоинту через response_format.
Слой 2 — Pydantic: запрет пустых строк через AfterValidator (НЕ Field(min_length=1),
         иначе minLength утёк бы в JSON-схему — часть эндпоинтов strict-mode его
         отклоняет), плюс непустота models.source_urls.

Контракт совпадает с разделом «Структура итогового JSON» в PARTS_RESEARCH_SPEC.md."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, model_validator


def _nonempty(v: str) -> str:
    if not v.strip():
        raise ValueError("string must be non-empty")
    return v


NonEmptyStr = Annotated[str, AfterValidator(_nonempty)]


class WeightBlock(BaseModel):
    kg: float
    source_url: NonEmptyStr
    evidence: NonEmptyStr


class ModelsBlock(BaseModel):
    text: NonEmptyStr
    source_urls: list[NonEmptyStr]
    evidence: NonEmptyStr

    @model_validator(mode="after")
    def _urls_present(self) -> "ModelsBlock":
        if not self.source_urls:
            raise ValueError("models.source_urls must contain at least one url")
        return self


class ArticleItem(BaseModel):
    article: NonEmptyStr
    source_url: NonEmptyStr
    evidence: NonEmptyStr


class LowConfidenceItem(BaseModel):
    article: NonEmptyStr
    source_url: NonEmptyStr
    evidence: NonEmptyStr
    why_low_confidence: NonEmptyStr


class IrrelevantItem(BaseModel):
    article: NonEmptyStr
    source_url: NonEmptyStr
    evidence: NonEmptyStr
    why_irrelevant: NonEmptyStr


class NumbersBlock(BaseModel):
    article: list[ArticleItem]
    article_low_confidence: list[LowConfidenceItem]
    irrelevant: list[IrrelevantItem]


class KitComponent(BaseModel):
    article: NonEmptyStr | None  # null, если артикул компонента не найден
    name: NonEmptyStr
    name_en: NonEmptyStr | None  # английское имя компонента (-> smart.parts_en)
    quantity: int | None
    description: NonEmptyStr | None
    description_en: NonEmptyStr | None
    source_url: NonEmptyStr
    evidence: NonEmptyStr


class PriceOffer(BaseModel):
    """Цена за ОРИГИНАЛ на US-магазине: текущая цена продажи именно нашего номера
    (Price/Now), не MSRP, не порог доставки, не цена другого/substitute товара."""
    site: NonEmptyStr           # магазин (домен), напр. "partsvu.com"
    price: float
    currency: NonEmptyStr | None  # null -> USD
    url: NonEmptyStr            # ссылка на товарную страницу
    article: NonEmptyStr        # OEM-номер, по которому найдена цена
    in_stock: bool | None
    evidence: NonEmptyStr       # цитата-обоснование (откуда цена)


class PartOfKit(BaseModel):
    kit_article: NonEmptyStr | None  # null, если номер набора неизвестен
    kit_name: NonEmptyStr | None
    source_url: NonEmptyStr
    evidence: NonEmptyStr


class StructuredResult(BaseModel):
    task_part_number: NonEmptyStr
    name: NonEmptyStr | None
    name_en: NonEmptyStr | None  # английское имя (-> smart.parts_en.name)
    brand_oem: list[NonEmptyStr]
    vehicle_classes: list[NonEmptyStr]  # слаги smart.vehicle_classes; [] = не определены
    description: NonEmptyStr | None
    description_en: NonEmptyStr | None  # английское описание (-> smart.parts_en.description)
    weight: WeightBlock | None
    models: ModelsBlock | None
    is_kit: bool
    kit_contents: list[KitComponent]
    part_of_kits: list[PartOfKit]
    numbers: NumbersBlock
    us_prices: list[PriceOffer]  # US-цены за оригинал; [] = не найдено в turn-1 -> фолбэк
