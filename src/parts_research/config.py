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


@dataclass(frozen=True)
class Settings:
    database_url: str
    llm_base_url: str
    llm_api_key: str
    llm_model_research: str
    llm_model_curator: str
    exa_api_key: str
    worker_concurrency: int
    worker_stale_minutes: int
    # Формат-валидация артикулов: 'soft' (проблемы пишем, run/part не валим — раскат и
    # сбор бэклога правил) | 'hard' (критич.фейл на NO_RULE тоже). NOT_CANONICAL валит
    # ВСЕГДА в обоих режимах (доказанно грязная форма).
    format_validation_mode: str


def load_settings() -> Settings:
    return Settings(
        database_url=_require("PARTS_RESEARCH_DATABASE_URL"),
        llm_base_url=_require("LLM_BASE_URL"),
        llm_api_key=_require("LLM_API_KEY"),
        llm_model_research=_require("LLM_MODEL_RESEARCH"),
        # Куратор по умолчанию использует ту же модель, что и research.
        llm_model_curator=os.environ.get("LLM_MODEL_CURATOR") or _require("LLM_MODEL_RESEARCH"),
        exa_api_key=_require("EXA_API_KEY"),
        worker_concurrency=int(os.environ.get("WORKER_CONCURRENCY", "30")),
        worker_stale_minutes=int(os.environ.get("WORKER_STALE_MINUTES", "30")),
        format_validation_mode=(os.environ.get("FORMAT_VALIDATION_MODE") or "soft").lower(),
    )


# Загружается один раз при импорте — импорт упадёт сразу, если env неполон.
settings = load_settings()
