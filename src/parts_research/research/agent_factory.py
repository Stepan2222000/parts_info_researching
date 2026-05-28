"""Фабрика research-агента. Один OpenAIChatCompletionsModel переиспользуется
всеми turn'ами; фаза 1 — без тулов, фаза 2 — с web_search_exa/web_fetch_exa.
Tracing отключаем (эндпоинт не openai.com)."""

from __future__ import annotations

from typing import Any

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
from .schema import StructuredResult

set_tracing_disabled(True)

_client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
_model = OpenAIChatCompletionsModel(model=settings.llm_model_research, openai_client=_client)
# Штатный SDK-retry на transient transport errors (не ловит обрывы посреди
# стрима — их добирает turn-level retry-loop в streaming.py).
_retry = ModelSettings(retry=ModelRetrySettings(max_retries=2, policy=retry_policies.network_error()))


def make_research_agent(system_prompt: str, tools: list[Any] | None = None) -> Agent:
    return Agent(
        name="research",
        instructions=system_prompt,
        model=_model,
        model_settings=_retry,
        output_type=StructuredResult,
        tools=tools or [],
    )
