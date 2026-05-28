"""Доменные исключения research-pipeline. Маппинг на статусы run'а — в run.py:
NoExactDataError -> failed_no_data; ValidationError/ValueError/MaxTurnsExceeded
-> failed_validation; любое другое -> failed_crashed."""

from __future__ import annotations


class NoExactDataError(Exception):
    """Exa не вернул источников с точным вхождением артикула (-> failed_no_data)."""
