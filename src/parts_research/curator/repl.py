"""CLI REPL куратора (Этап 3).

Каждая сессия = строка в curator_sessions + PostgresSession(curator_<id>). Перед
каждым ответом в начало user-сообщения подмешивается `<queue>`-snapshot. История
Agents SDK ведётся через PostgresSession; визуальная история (user/assistant/tool)
дублируется в curator_messages. Команды: /exit, /new.

run_once(message) — неинтерактивный one-shot (для прогонов и UI-обвязки)."""

from __future__ import annotations

import asyncio
import json
import sys

import asyncpg
from exa_py import AsyncExa

from agents import ItemHelpers, Runner

from ..config import settings
from ..db.pool import create_pool
from ..db.session import PostgresSession
from .agent_factory import build_curator_system_prompt, make_curator_agent
from .snapshot import format_snapshot, load_snapshot
from .tools import CuratorRunContext


def out(msg: str = "") -> None:
    print(msg, flush=True)


async def _load_allowed(pool: asyncpg.Pool) -> tuple[list[str], list[str]]:
    brands, types = await asyncio.gather(
        pool.fetch("SELECT name FROM smart.brands ORDER BY name"),
        pool.fetch("SELECT name FROM smart.product_types ORDER BY name"),
    )
    return [r["name"] for r in brands], [r["name"] for r in types]


async def _new_session(pool: asyncpg.Pool) -> int:
    return await pool.fetchval("INSERT INTO curator_sessions (started_at) VALUES (now()) RETURNING id")


async def _persist_events(streamed, pool: asyncpg.Pool, session_id: int) -> None:
    """Рендер stream-событий в stdout + дублирование в curator_messages."""
    pending: dict = {}
    async for ev in streamed.stream_events():
        if ev.type != "run_item_stream_event":
            continue
        item = ev.item
        if item.type == "tool_call_item":
            raw = item.raw_item
            name = getattr(raw, "name", None) or getattr(getattr(raw, "function", None), "name", None) or "tool"
            args = getattr(raw, "arguments", None) or getattr(getattr(raw, "function", None), "arguments", None) or ""
            cid = getattr(raw, "call_id", None) or getattr(raw, "id", None)
            pending[cid] = {"tool": name, "arguments": args}
            out(f"  → {name} {str(args)[:300]}")
        elif item.type == "tool_call_output_item":
            raw = item.raw_item
            cid = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", None)
            output = getattr(item, "output", None)
            if output is None and isinstance(raw, dict):
                output = raw.get("output")
            call = pending.pop(cid, {"tool": "?", "arguments": None})
            out(f"  ← {call['tool']} -> {str(output)[:500]}")
            await pool.execute(
                "INSERT INTO curator_messages (session_id, role, tool_call) VALUES ($1, 'tool', $2)",
                session_id, {**call, "output": output},
            )
        elif item.type == "message_output_item":
            text = ItemHelpers.text_message_output(item)
            out(f"\n{text}\n")
            await pool.execute(
                "INSERT INTO curator_messages (session_id, role, content) VALUES ($1, 'assistant', $2)",
                session_id, text,
            )


async def _run_turn(agent, session, ctx: CuratorRunContext, pool: asyncpg.Pool, session_id: int, user_text: str) -> None:
    snapshot = format_snapshot(await load_snapshot(pool))
    await pool.execute(
        "INSERT INTO curator_messages (session_id, role, content) VALUES ($1, 'user', $2)", session_id, user_text)
    input_text = f"{snapshot}\n\n{user_text}"
    try:
        streamed = Runner.run_streamed(agent, input=input_text, session=session, context=ctx)
        await _persist_events(streamed, pool, session_id)
    except Exception as e:  # noqa: BLE001 — ошибку показываем, REPL не роняем
        out(f"[turn error] {type(e).__name__}: {e}")


async def _build(pool: asyncpg.Pool):
    brands, types = await _load_allowed(pool)
    agent = make_curator_agent(build_curator_system_prompt(brands, types))
    exa = AsyncExa(api_key=settings.exa_api_key)
    return agent, exa


async def run_repl() -> None:
    pool = await create_pool(min_size=2, max_size=10)
    try:
        agent, exa = await _build(pool)
        session_id = await _new_session(pool)
        session = PostgresSession(f"curator_{session_id}", pool)
        ctx = CuratorRunContext(pool=pool, exa=exa, session_id=session_id)
        out(f"[curator] session={session_id}. Команды: /exit, /new. Пиши сообщение.")
        while True:
            try:
                line = (await asyncio.to_thread(input, "curator> ")).strip()
            except (EOFError, KeyboardInterrupt):
                out("\n[curator] bye")
                break
            if not line:
                continue
            if line == "/exit":
                break
            if line == "/new":
                await pool.execute("UPDATE curator_sessions SET ended_at=now() WHERE id=$1", session_id)
                session_id = await _new_session(pool)
                session = PostgresSession(f"curator_{session_id}", pool)
                ctx = CuratorRunContext(pool=pool, exa=exa, session_id=session_id)
                out(f"[curator] new session={session_id}")
                continue
            await _run_turn(agent, session, ctx, pool, session_id, line)
        await pool.execute("UPDATE curator_sessions SET ended_at=now() WHERE id=$1 AND ended_at IS NULL", session_id)
    finally:
        await pool.close()


async def run_once(message: str) -> None:
    """Неинтерактивный one-shot: одна сессия, одно сообщение, печать стрима, выход."""
    pool = await create_pool(min_size=2, max_size=10)
    try:
        agent, exa = await _build(pool)
        session_id = await _new_session(pool)
        session = PostgresSession(f"curator_{session_id}", pool)
        ctx = CuratorRunContext(pool=pool, exa=exa, session_id=session_id)
        out(f"[curator one-shot] session={session_id}")
        await _run_turn(agent, session, ctx, pool, session_id, message)
        await pool.execute("UPDATE curator_sessions SET ended_at=now() WHERE id=$1", session_id)
    finally:
        await pool.close()
