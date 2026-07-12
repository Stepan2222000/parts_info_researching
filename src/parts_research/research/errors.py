"""Доменные исключения research-pipeline. Маппинг на статусы run'а — в run.py:
NoExactDataError -> failed_no_data; ValidationError/ValueError/MaxTurnsExceeded
-> failed_validation; любое другое -> failed_crashed."""

from __future__ import annotations


class NoExactDataError(Exception):
    """Exa не вернул источников с точным вхождением артикула (-> failed_no_data)."""


class StructuredOutputInvalid(ValueError):
    """Финальный вывод модели не разобрался в StructuredResult (битый JSON либо
    несоответствие pydantic-схеме). Подкласс ValueError — статус-маппинг прежний
    (failed_validation); отдельный тип нужен repair-циклу: такая ошибка означает
    «модель ответила, но ответ невалиден», и её можно вернуть агенту на исправление."""
