"""Фабрика curator-агента + сборка системного промпта.

Системный промпт = правила `curator_rules.md` + допустимые Smart-бренды и product_types
(из Smart через FDW). Тулы — `@function_tool` в том же процессе (без MCP). Модель —
`settings.llm_model_curator`. Tracing off (эндпоинт не openai.com)."""

from __future__ import annotations

import os
from pathlib import Path

from openai import AsyncOpenAI

from agents import (
    Agent,
    ModelSettings,
    OpenAIChatCompletionsModel,
    retry_policies,
    set_tracing_disabled,
)
from agents.model_settings import ModelRetrySettings

from ..config import settings
from .tools import (
    execute_sql,
    mark_needs_review,
    save_to_smart,
    web_fetch_exa,
    web_search_exa,
)

set_tracing_disabled(True)

# curator_rules.md лежит в корне репо; путь переопределяется CURATOR_RULES_PATH.
_DEFAULT_RULES_PATH = Path(__file__).resolve().parents[3] / "curator_rules.md"
RULES_PATH = Path(os.environ.get("CURATOR_RULES_PATH", _DEFAULT_RULES_PATH))

_client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
_model = OpenAIChatCompletionsModel(model=settings.llm_model_curator, openai_client=_client)
_retry = ModelSettings(retry=ModelRetrySettings(max_retries=2, policy=retry_policies.network_error()))


def build_curator_system_prompt(allowed_brands: list[str], allowed_product_types: list[str]) -> str:
    rules = RULES_PATH.read_text(encoding="utf-8")
    return f"""\
{rules}

--- Справочник Smart (для записи) ---
Допустимые product_type: {", ".join(allowed_product_types)}
Допустимые Smart-бренды (brands, UPPER_SNAKE_CASE): {", ".join(allowed_brands)}
Бренд для save_to_smart должен быть из этого списка (иначе FK-ошибка по конкретному part).
--- Конец справочника ---"""


def make_curator_agent(system_prompt: str) -> Agent:
    return Agent(
        name="curator",
        instructions=system_prompt,
        model=_model,
        model_settings=_retry,
        tools=[execute_sql, save_to_smart, mark_needs_review, web_search_exa, web_fetch_exa],
    )
