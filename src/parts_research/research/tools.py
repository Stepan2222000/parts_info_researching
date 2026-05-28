"""Тулы фазы 2 (@function_tool). Внутри — тот же кэширующий Exa-клиент с
phase='agent_extra'. Счётчик вызовов живёт в RunContextWrapper.context
(Phase2Ctx), один tool_call = один шаг счётчика независимо от числа urls.
По достижении лимита тул возвращает модели текст «финализируй», а не новый вызов."""

from __future__ import annotations

import json
from dataclasses import dataclass

import asyncpg
from exa_py import AsyncExa

from agents import RunContextWrapper, function_tool

from .exa_client import cached_exa_call, pick_fetch, pick_search

PHASE2_EXA_LIMIT = 10

_LIMIT_MSG = "Лимит Exa-вызовов исчерпан. Финализируй JSON по схеме без новых вызовов тулов."


@dataclass
class Phase2Ctx:
    pool: asyncpg.Pool
    exa: AsyncExa
    run_id: int
    limit: int = PHASE2_EXA_LIMIT
    exa_calls: int = 0


@function_tool
async def web_search_exa(
    wrapper: RunContextWrapper[Phase2Ctx], query: str, num_results: int = 10
) -> str:
    """Search the web via Exa for sources about the task article.

    Use this when the current JSON has empty or doubtful fields you want to fill.
    Returns a compact JSON array of {url, title, highlights}.

    Args:
        query: Natural-language or keyword search query.
        num_results: How many results to return (default 10).
    """
    ctx = wrapper.context
    if ctx.exa_calls >= ctx.limit:
        return _LIMIT_MSG
    ctx.exa_calls += 1
    raw = await cached_exa_call(
        ctx.pool, ctx.exa, "web_search_exa",
        {"query": query, "num_results": num_results},
        run_id=ctx.run_id, phase="agent_extra",
    )
    return json.dumps(pick_search(raw), ensure_ascii=False)


@function_tool
async def web_fetch_exa(
    wrapper: RunContextWrapper[Phase2Ctx], urls: list[str], max_characters: int = 3000
) -> str:
    """Fetch the full text of specific URLs via Exa.

    Use this to read a promising source in detail. Returns a compact JSON array
    of {url, title, text}, each text truncated to max_characters.

    Args:
        urls: URLs to fetch (batch several in one call).
        max_characters: Max characters of text per page (default 3000).
    """
    ctx = wrapper.context
    if ctx.exa_calls >= ctx.limit:
        return _LIMIT_MSG
    ctx.exa_calls += 1
    raw = await cached_exa_call(
        ctx.pool, ctx.exa, "web_fetch_exa", {"urls": urls},
        run_id=ctx.run_id, phase="agent_extra",
    )
    return json.dumps(pick_fetch(raw, max_characters), ensure_ascii=False)
