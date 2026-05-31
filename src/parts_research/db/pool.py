"""asyncpg-пул к parts_research.

Ключевой момент: asyncpg по умолчанию отдаёт jsonb/json как `str`. Чтобы
по всему коду работать со словарями (result_json, agent_history.item,
exa_cache.response и т.п.), на каждом новом соединении регистрируем codec
json/jsonb <-> dict. ensure_ascii=False — чтобы кириллица в evidence читалась
в БД как есть."""

from __future__ import annotations

import json

import asyncpg

from ..config import settings


def strip_nul(value: object) -> object:
    """Рекурсивно вырезает байт \\u0000 из всех строк структуры.

    Postgres text/jsonb физически не хранит NUL (UntranslatableCharacterError:
    "\\u0000 cannot be converted to text"). NUL прилетает в сыром тексте страниц
    из Exa-скрейпа и роняет INSERT (весь run -> failed_crashed). Чистим перед
    записью. На ключ кэша не влияет — request_hash считается отдельно от args.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [strip_nul(v) for v in value]
    if isinstance(value, tuple):
        return tuple(strip_nul(v) for v in value)
    if isinstance(value, dict):
        return {k: strip_nul(v) for k, v in value.items()}
    return value


def _dumps(value: object) -> str:
    return json.dumps(strip_nul(value), ensure_ascii=False)


async def _init_connection(conn: asyncpg.Connection) -> None:
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(
            type_name,
            encoder=_dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def create_pool(*, min_size: int = 10, max_size: int = 60) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        settings.database_url,
        min_size=min_size,
        max_size=max_size,
        init=_init_connection,
    )
