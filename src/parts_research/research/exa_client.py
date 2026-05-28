"""Кэширующий Exa-клиент. Все Exa-вызовы (backend-Exa фазы 1 и агентские тулы
фазы 2) идут через cached_exa_call: hash от (tool_name + canonical args) ->
exa_cache; на промахе зовём реальный Exa и сохраняем СЫРОЙ ответ целиком.
Pick/обрезка под нужды модели применяются на чтении (pick_search/pick_fetch).

Никаких файлов на диске — только БД."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import asyncpg
from exa_py import AsyncExa


def _to_jsonable(obj: Any) -> Any:
    """Рекурсивно превращает объекты exa-py (не pydantic) в JSON-совместимое.

    Result/SearchResponse хранят данные в __dict__; всё остальное — примитивы,
    списки, словари; неизвестное падает в str()."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return {k: _to_jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _canonical_hash(tool_name: str, args: dict[str, Any]) -> str:
    blob = tool_name + "\n" + json.dumps(
        args, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def _call_exa(exa: AsyncExa, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "web_search_exa":
        response = await exa.search(
            args["query"],
            num_results=int(args.get("num_results", 10)),
            contents={"highlights": True},
        )
    elif tool_name == "web_fetch_exa":
        response = await exa.get_contents(args["urls"], text=True)
    else:
        raise ValueError(f"unknown exa tool_name: {tool_name!r}")
    return _to_jsonable(response)


async def cached_exa_call(
    pool: asyncpg.Pool,
    exa: AsyncExa,
    tool_name: str,
    args: dict[str, Any],
    *,
    run_id: int | None = None,
    phase: str | None = None,
) -> dict[str, Any]:
    """Возвращает СЫРОЙ ответ Exa (dict). Кэш — exact-match по (tool_name, args).

    args — только то, что влияет на ответ: search -> {query, num_results};
    fetch -> {urls}. Презентационные параметры (max_characters) в ключ не входят.
    """
    request_hash = _canonical_hash(tool_name, args)

    row = await pool.fetchrow(
        "SELECT id, response FROM exa_cache WHERE request_hash = $1", request_hash
    )
    if row is not None:
        cache_id = row["id"]
        response = row["response"]
        hit = True
        await pool.execute(
            "UPDATE exa_cache SET hit_count = hit_count + 1, last_used_at = now() WHERE id = $1",
            cache_id,
        )
    else:
        # Реальный сетевой вызов вне транзакции (не держим соединение).
        response = await _call_exa(exa, tool_name, args)
        cache_id = await pool.fetchval(
            "INSERT INTO exa_cache (request_hash, tool_name, arguments, response) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (request_hash) DO UPDATE SET last_used_at = now() RETURNING id",
            request_hash,
            tool_name,
            args,
            response,
        )
        hit = False

    if run_id is not None:
        await pool.execute(
            "INSERT INTO exa_cache_usage (cache_id, run_id, phase, hit) VALUES ($1, $2, $3, $4)",
            cache_id,
            run_id,
            phase,
            hit,
        )
    return response


def pick_search(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Компактный pick из search-ответа: url/title/highlights."""
    return [
        {"url": r.get("url"), "title": r.get("title"), "highlights": r.get("highlights")}
        for r in response.get("results", [])
    ]


def pick_fetch(response: dict[str, Any], max_characters: int) -> list[dict[str, Any]]:
    """Компактный pick из contents-ответа: url/title/text (обрезанный)."""
    out: list[dict[str, Any]] = []
    for r in response.get("results", []):
        text = (r.get("text") or "")[:max_characters]
        out.append({"url": r.get("url"), "title": r.get("title"), "text": text})
    return out
