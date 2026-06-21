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
с текстом, лог в stderr И запись в curator_messages (assistant, ⚠️-пометка),
чтобы упавший turn был виден после перезагрузки, а не выглядел «зависшим».

KEEP-ALIVE (важно): шаг модели с большим вызовом тула (напр. save_to_smart на
много деталей у gpt-5.5(high)) генерируется ~80с, и всё это время наружу не уходит
НИ одного видимого чанка (reasoning и дельты аргументов тула мы не форвардим).
Тихий SSE рвётся idle-таймаутом в цепочке браузер→Next→FastAPI → FastAPI отменяет
генератор → turn гибнет на полуслове (save_to_smart не доезжает, сохраняется 0).
Лечим: стрим агента крутится в фоновом таске и пишет чанки в очередь, а отдающий
генератор ждёт чанк с таймаутом и при простое шлёт SSE-комментарий-пинг — это
держит соединение живым на любом хопе (комментарии `:`-строк SSE-парсер игнорит)."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import AsyncIterator

import asyncpg

from agents import ItemHelpers, Runner

from ..curator.snapshot import format_snapshot, load_snapshot
from ..curator.tools import CuratorRunContext
from ..db.session import PostgresSession

# Пинг чаще любого разумного idle-таймаута (прокси/LB обычно 30-60с).
_KEEPALIVE_SECS = 10.0
_KEEPALIVE = ": keep-alive\n\n"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


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
) -> AsyncIterator[str]:
    """Async-генератор SSE-строк протокола v6 на один turn куратора.

    Стрим агента крутится в фоновом producer-таске (пишет готовые SSE-строки в
    очередь), а сам генератор отдаёт их клиенту, вставляя keep-alive-пинги при
    простое — иначе долгий тихий шаг (большой save_to_smart) рвёт SSE по таймауту.
    """
    session = PostgresSession(f"curator_{session_id}", pool)
    ctx = CuratorRunContext(pool=pool, exa=exa, session_id=session_id)

    # Snapshot очереди подмешивается в начало каждого user-сообщения (как в REPL/спеке).
    snapshot = format_snapshot(await load_snapshot(pool))
    input_text = f"{snapshot}\n\n{user_text}"

    # Визуальная история: user-сообщение пишем сразу (без snapshot — это служебное).
    await pool.execute(
        "INSERT INTO curator_messages (session_id, role, content) VALUES ($1, 'user', $2)",
        session_id, user_text,
    )

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def produce() -> None:
        """Крутит стрим агента, кладёт готовые SSE-строки в очередь. Дублирует
        assistant-текст/tool-calls в curator_messages. Ошибку логирует, шлёт
        чанком error И сохраняет как assistant-сообщение (видно после reload).
        Завершается, положив None-сентинел."""
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
                            await queue.put(_sse({"type": "text-start", "id": text_id}))
                        await queue.put(_sse({"type": "text-delta", "id": text_id, "delta": delta}))
                    continue

                if ev.type != "run_item_stream_event":
                    continue

                item = ev.item
                if item.type == "tool_call_item":
                    # Закрываем открытый текстовый блок перед tool-частью.
                    if text_id is not None:
                        await queue.put(_sse({"type": "text-end", "id": text_id}))
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
                    await queue.put(_sse({"type": "tool-input-start", "toolCallId": call_id,
                                          "toolName": name, "dynamic": True}))
                    await queue.put(_sse(
                        {"type": "tool-input-available", "toolCallId": call_id,
                         "toolName": name, "input": parsed_input, "dynamic": True}
                    ))

                elif item.type == "tool_call_output_item":
                    raw = item.raw_item
                    call_id = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", None)
                    output = getattr(item, "output", None)
                    if output is None and isinstance(raw, dict):
                        output = raw.get("output")
                    call = pending_tools.pop(call_id, {"tool": "tool", "arguments": None})
                    await queue.put(_sse({"type": "tool-output-available", "toolCallId": call_id,
                                          "output": output, "dynamic": True}))
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
                await queue.put(_sse({"type": "text-end", "id": text_id}))
                text_id = None

        except Exception as e:  # noqa: BLE001 — ошибку показываем текстом, логируем и сохраняем
            _log(f"[curator session={session_id}] turn error: {type(e).__name__}: {e}")
            if text_id is not None:
                await queue.put(_sse({"type": "text-end", "id": text_id}))
                text_id = None
            await queue.put(_sse({"type": "error", "errorText": f"{type(e).__name__}: {e}"}))
            # Персистим как assistant — иначе после reload turn выглядит «зависшим».
            try:
                await pool.execute(
                    "INSERT INTO curator_messages (session_id, role, content) VALUES ($1, 'assistant', $2)",
                    session_id, f"⚠️ Turn прерван: {type(e).__name__}: {e}",
                )
            except Exception as persist_err:  # noqa: BLE001
                _log(f"[curator session={session_id}] failed to persist error: {persist_err}")
        finally:
            await queue.put(None)  # сентинел: producer завершился

    yield _sse({"type": "start", "messageId": _new_id("msg")})
    yield _sse({"type": "start-step"})

    task = asyncio.create_task(produce())
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECS)
            except asyncio.TimeoutError:
                yield _KEEPALIVE  # держим соединение живым на тихом шаге
                continue
            if chunk is None:
                break
            yield chunk
    finally:
        if not task.done():
            task.cancel()

    yield _sse({"type": "finish-step"})
    yield _sse({"type": "finish"})
    yield _sse("[DONE]")
