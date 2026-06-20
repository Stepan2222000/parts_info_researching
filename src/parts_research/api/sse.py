"""Маппер стрима curator-агента (Agents SDK) → AI SDK v6 UI Message Stream.

Эмпирически выверено (см. прогон этапа 4): события Agents SDK мапятся в чанки v6 так:
  raw_response_event / ResponseTextDeltaEvent  -> text-start / text-delta / text-end
  run_item tool_call_item (ResponseFunctionToolCall) -> tool-input-start / tool-input-available
  run_item tool_call_output_item               -> tool-output-available
Каркас сообщения: start -> start-step -> (parts) -> finish-step -> finish -> [DONE].

Заголовок ответа должен быть x-vercel-ai-ui-message-stream: v1 (ставит app.py).
SSE-кадр = "data: {json}\\n\\n"; поток закрывается "data: [DONE]\\n\\n".

Параллельно с эмитом чанков дублируем assistant-текст и tool-calls в
curator_messages (визуальная история чата) — как в CLI REPL.

Ошибки не скрываем: обрыв/исключение посреди стрима → чанк {"type":"error",...}
с текстом, затем корректное закрытие потока."""

from __future__ import annotations

import json
import uuid
from typing import AsyncIterator

import asyncpg

from agents import ItemHelpers, Runner

from ..curator.snapshot import format_snapshot, load_snapshot
from ..curator.tools import CuratorRunContext
from ..db.session import PostgresSession


def _sse(obj: dict | str) -> str:
    if isinstance(obj, str):
        return f"data: {obj}\n\n"
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def curator_event_stream(
    *,
    agent,
    pool: asyncpg.Pool,
    exa,
    session_id: int,
    user_text: str,
    prices_pool: asyncpg.Pool | None = None,
) -> AsyncIterator[str]:
    """Async-генератор SSE-строк протокола v6 на один turn куратора."""
    session = PostgresSession(f"curator_{session_id}", pool)
    ctx = CuratorRunContext(pool=pool, exa=exa, session_id=session_id, prices_pool=prices_pool)

    # Snapshot очереди подмешивается в начало каждого user-сообщения (как в REPL/спеке).
    snapshot = format_snapshot(await load_snapshot(pool))
    input_text = f"{snapshot}\n\n{user_text}"

    # Визуальная история: user-сообщение пишем сразу (без snapshot — это служебное).
    await pool.execute(
        "INSERT INTO curator_messages (session_id, role, content) VALUES ($1, 'user', $2)",
        session_id, user_text,
    )

    yield _sse({"type": "start", "messageId": _new_id("msg")})
    yield _sse({"type": "start-step"})

    text_id: str | None = None
    pending_tools: dict = {}  # call_id -> {tool, arguments}

    try:
        streamed = Runner.run_streamed(agent, input=input_text, session=session, context=ctx)
        async for ev in streamed.stream_events():
            if ev.type == "raw_response_event":
                data = ev.data
                if getattr(data, "type", None) == "response.output_text.delta":
                    delta = getattr(data, "delta", "") or ""
                    if not delta:
                        continue
                    if text_id is None:
                        text_id = _new_id("txt")
                        yield _sse({"type": "text-start", "id": text_id})
                    yield _sse({"type": "text-delta", "id": text_id, "delta": delta})
                continue

            if ev.type != "run_item_stream_event":
                continue

            item = ev.item
            if item.type == "tool_call_item":
                # Закрываем открытый текстовый блок перед tool-частью.
                if text_id is not None:
                    yield _sse({"type": "text-end", "id": text_id})
                    text_id = None
                raw = item.raw_item
                name = (
                    getattr(raw, "name", None)
                    or getattr(getattr(raw, "function", None), "name", None)
                    or "tool"
                )
                arguments = (
                    getattr(raw, "arguments", None)
                    or getattr(getattr(raw, "function", None), "arguments", None)
                    or ""
                )
                call_id = getattr(raw, "call_id", None) or getattr(raw, "id", None) or _new_id("call")
                try:
                    parsed_input = json.loads(arguments) if arguments else {}
                except (json.JSONDecodeError, TypeError):
                    parsed_input = {"_raw_arguments": arguments}
                pending_tools[call_id] = {"tool": name, "arguments": arguments}
                # dynamic=true: у клиента нет схем тулов → часть рендерится как dynamic-tool.
                yield _sse({"type": "tool-input-start", "toolCallId": call_id,
                            "toolName": name, "dynamic": True})
                yield _sse(
                    {"type": "tool-input-available", "toolCallId": call_id,
                     "toolName": name, "input": parsed_input, "dynamic": True}
                )

            elif item.type == "tool_call_output_item":
                raw = item.raw_item
                call_id = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", None)
                output = getattr(item, "output", None)
                if output is None and isinstance(raw, dict):
                    output = raw.get("output")
                call = pending_tools.pop(call_id, {"tool": "tool", "arguments": None})
                yield _sse({"type": "tool-output-available", "toolCallId": call_id,
                            "output": output, "dynamic": True})
                await pool.execute(
                    "INSERT INTO curator_messages (session_id, role, tool_call) VALUES ($1, 'tool', $2)",
                    session_id, {**call, "output": output},
                )

            elif item.type == "message_output_item":
                text = ItemHelpers.text_message_output(item)
                await pool.execute(
                    "INSERT INTO curator_messages (session_id, role, content) VALUES ($1, 'assistant', $2)",
                    session_id, text,
                )

        if text_id is not None:
            yield _sse({"type": "text-end", "id": text_id})
            text_id = None

    except Exception as e:  # noqa: BLE001 — ошибку показываем текстом, без фолбеков
        if text_id is not None:
            yield _sse({"type": "text-end", "id": text_id})
            text_id = None
        yield _sse({"type": "error", "errorText": f"{type(e).__name__}: {e}"})

    yield _sse({"type": "finish-step"})
    yield _sse({"type": "finish"})
    yield _sse("[DONE]")
