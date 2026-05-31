"""Кэширующий Exa-клиент. Все Exa-вызовы (backend-Exa фазы 1 и агентские тулы
фазы 2) идут через cached_exa_call: hash от (tool_name + canonical args) ->
exa_cache; на промахе зовём реальный Exa и сохраняем СЫРОЙ ответ целиком.
Pick/обрезка под нужды модели применяются на чтении (pick_search/pick_fetch).

Никаких файлов на диске — только БД."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import deque
from typing import Any

import asyncpg
from exa_py import AsyncExa

# Глобальный троттл РЕАЛЬНЫХ Exa-вызовов (Exa лимитит 10 req/s). Скользящее окно
# 1 сек; применяется только на cache-miss — попадания в кэш Exa не дёргают.
# Один процесс = один лимитер (общий для всех конкурентных run'ов процесса).
EXA_MAX_REQUESTS_PER_SEC = int(os.environ.get("EXA_MAX_REQUESTS_PER_SEC", "10"))


class _ExaRateLimiter:
    def __init__(self, max_per_sec: int) -> None:
        self._max = max_per_sec
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 1.0:
                    self._calls.popleft()
                if len(self._calls) < self._max:
                    self._calls.append(now)
                    return
                await asyncio.sleep(1.0 - (now - self._calls[0]))


_exa_limiter = _ExaRateLimiter(EXA_MAX_REQUESTS_PER_SEC)


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
        search_kwargs: dict[str, Any] = {
            "num_results": int(args.get("num_results", 10)),
            "contents": {"highlights": True},
        }
        # type="keyword" — точный поиск по номеру (нейронный режим тащил похожие номера).
        if args.get("type"):
            search_kwargs["type"] = args["type"]
        response = await exa.search(args["query"], **search_kwargs)
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
        # Глобальный троттл, чтобы не ловить Exa 429 (10 req/s) при высокой параллельности.
        await _exa_limiter.acquire()
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
