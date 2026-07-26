"""Чтение конфигурации из окружения (.env). Fail-fast: отсутствие обязательной
переменной — это явная ошибка на старте, а не молчаливый дефолт."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required env var: {name}")
    return value


def _bool_env(name: str, default: bool) -> bool:
    """Строгий парсинг булевого флага: непонятное значение — ошибка старта, не дефолт."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "on", "yes"):
        return True
    if value in ("0", "false", "off", "no"):
        return False
    raise RuntimeError(f"env var {name} must be boolean (1/0/true/false/on/off/yes/no), got {raw!r}")


@dataclass(frozen=True)
class Settings:
    database_url: str
    llm_base_url: str
    llm_api_key: str
    llm_model_research: str
    llm_model_curator: str
    # Дешёвая модель «судьи» воронки похожести (анти-дубли) и разового бэкфилла
    # классов: примитивная классификация без web, дорогая модель не нужна.
    llm_model_judge: str
    exa_api_key: str
    worker_concurrency: int
    worker_stale_minutes: int
    # Формат-валидация артикулов: 'soft' (проблемы пишем, run/part не валим — раскат и
    # сбор бэклога правил) | 'hard' (критич.фейл на NO_RULE тоже). NOT_CANONICAL валит
    # ВСЕГДА в обоих режимах (доказанно грязная форма).
    format_validation_mode: str
    # Дефолт repair-флага профиля: упавшую валидацию этапа возвращаем агенту на
    # исправление (1 попытка, тем же session'ом) вместо немедленного фейла рана.
    # Дефолт применяется при resolve_profile, когда submit не передал "repair" явно.
    research_repair_validation: bool
    # Дефолт auto_publish-флага профиля: done-ран сразу публикуется в smart без
    # куратора (только однозначные случаи — «зелёный коридор», см. auto_publish.py;
    # остальное остаётся куратору). Применяется, когда submit не передал флаг явно.
    research_auto_publish: bool
    # Потолок числа деталей, которые curator-тул get_context разворачивает за один вызов
    # (защита от раздувания контекста). Превышение -> усечение с пометкой truncated.
    curator_get_context_max_parts: int
    # LLM-судья воронки похожести после авто-INSERT: on -> шорт-лист прогоняется через
    # llm_model_judge, вердикты same/likely_same пишутся в dedup_candidates. off -> LLM
    # не вызывается вообще (детерминированный шорт-лист в outcome остаётся — он бесплатный).
    # Судья ничего не блокирует, флаг можно переключать в любой момент.
    research_dedup_judge: bool
    # Размер шорт-листа воронки похожести (замер 2026-07-26: recall настоящих дублей
    # в топ-80 = 98%, в топ-40 = 96.7%).
    dedup_shortlist_k: int


def load_settings() -> Settings:
    return Settings(
        database_url=_require("PARTS_RESEARCH_DATABASE_URL"),
        llm_base_url=_require("LLM_BASE_URL"),
        llm_api_key=_require("LLM_API_KEY"),
        llm_model_research=_require("LLM_MODEL_RESEARCH"),
        # Куратор по умолчанию использует ту же модель, что и research.
        llm_model_curator=os.environ.get("LLM_MODEL_CURATOR") or _require("LLM_MODEL_RESEARCH"),
        # Судья по умолчанию падает на модель куратора; в .env ставим дешёвую (gpt-5.6-luna).
        llm_model_judge=os.environ.get("LLM_MODEL_JUDGE")
        or os.environ.get("LLM_MODEL_CURATOR") or _require("LLM_MODEL_RESEARCH"),
        exa_api_key=_require("EXA_API_KEY"),
        worker_concurrency=int(os.environ.get("WORKER_CONCURRENCY", "30")),
        worker_stale_minutes=int(os.environ.get("WORKER_STALE_MINUTES", "30")),
        format_validation_mode=(os.environ.get("FORMAT_VALIDATION_MODE") or "soft").lower(),
        research_repair_validation=_bool_env("RESEARCH_REPAIR_VALIDATION", False),
        research_auto_publish=_bool_env("RESEARCH_AUTO_PUBLISH", False),
        curator_get_context_max_parts=int(os.environ.get("CURATOR_GET_CONTEXT_MAX_PARTS", "25")),
        research_dedup_judge=_bool_env("RESEARCH_DEDUP_JUDGE", True),
        dedup_shortlist_k=int(os.environ.get("DEDUP_SHORTLIST_K", "80")),
    )


# Загружается один раз при импорте — импорт упадёт сразу, если env неполон.
settings = load_settings()
