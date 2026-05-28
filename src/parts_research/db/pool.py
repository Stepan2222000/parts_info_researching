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


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


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
