"""PostgresSession — реализация Session-протокола Agents SDK поверх таблицы
agent_history. Используется и research-агентом (session_id='research_run_<id>'),
и куратором (session_id='curator_<id>').

Runner сам зовёт get_items() перед turn'ом и add_items() после (см.
prepare_input_with_session / save_result_to_session в agents.run). Items —
это TResponseInputItem (Responses-API-словари), кладутся в JSONB напрямую
благодаря codec'у из db.pool."""

from __future__ import annotations

import asyncpg
from agents.items import TResponseInputItem
from agents.memory.session import SessionABC


class PostgresSession(SessionABC):
    def __init__(self, session_id: str, pool: asyncpg.Pool) -> None:
        self.session_id = session_id
        self._pool = pool

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        if limit is None:
            rows = await self._pool.fetch(
                "SELECT item FROM agent_history WHERE session_id = $1 ORDER BY id",
                self.session_id,
            )
        else:
            # Последние `limit` items, но вернуть в хронологическом порядке.
            rows = await self._pool.fetch(
                "SELECT item FROM agent_history WHERE session_id = $1 "
                "ORDER BY id DESC LIMIT $2",
                self.session_id,
                limit,
            )
            rows = list(reversed(rows))
        return [row["item"] for row in rows]

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        if not items:
            return
        await self._pool.executemany(
            "INSERT INTO agent_history (session_id, item) VALUES ($1, $2)",
            [(self.session_id, item) for item in items],
        )

    async def pop_item(self) -> TResponseInputItem | None:
        row = await self._pool.fetchrow(
            "DELETE FROM agent_history WHERE id = ("
            "  SELECT id FROM agent_history WHERE session_id = $1 ORDER BY id DESC LIMIT 1"
            ") RETURNING item",
            self.session_id,
        )
        return row["item"] if row else None

    async def clear_session(self) -> None:
        await self._pool.execute(
            "DELETE FROM agent_history WHERE session_id = $1",
            self.session_id,
        )
