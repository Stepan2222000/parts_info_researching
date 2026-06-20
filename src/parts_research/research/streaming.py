"""Обёртка над Runner.run_streamed: дренирует stream-события, буферит их в
памяти и пишет в agent_stream_events ОДНИМ батчем только после успешного
завершения turn'а (обрыв/ошибка не оставляют строк — согласуется с тем, что
session тоже не сохраняет оборванный turn).

Поверх — собственный turn-level retry-loop: обрывы посреди стрима встроенный
SDK-retry не ловит (replay-safety), повторяем turn целиком. Повтор безопасен,
т.к. историю ведёт PostgresSession и оборванный turn в неё не попал."""

from __future__ import annotations

import re
import sys
from typing import Any

import asyncpg
import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

from agents import Runner
from agents.memory.session import SessionABC

from .schema import StructuredResult

TURN_NETWORK_RETRIES = 2

_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)


def _with_preamble(preamble: str | None, inp: Any) -> Any:
    """Префиксует строковый user-ввод преамбулой (схема+enum'ы+формат). Эндпоинт
    игнорирует system, поэтому критичные ограничения дублируем в КАЖДОМ user-сообщении.
    Нестроковый ввод / отсутствие преамбулы не трогаем."""
    if preamble and isinstance(inp, str):
        return f"{preamble}\n=== ЗАДАНИЕ ===\n{inp}"
    return inp


def _parse_structured_result(raw: Any) -> StructuredResult:
    """Финальный вывод модели -> StructuredResult. Эндпоинт не применяет
    response_format, поэтому модель отдаёт JSON (часто в markdown-обёртке) как
    текст: снимаем ```-обёртку / берём объект от первой { до последней }, чистим
    NUL и валидируем pydantic'ом. Невалид -> ValidationError -> failed_validation."""
    if isinstance(raw, StructuredResult):
        return raw
    text = (str(raw) if raw is not None else "").replace("\x00", "").strip()
    m = _FENCED_JSON.search(text)
    if m:
        js = m.group(1)
    else:
        i, j = text.find("{"), text.rfind("}")
        js = text[i:j + 1] if i >= 0 and j > i else text
    return StructuredResult.model_validate_json(js)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def is_transient(e: Exception) -> bool:
    """Транспортный обрыв соединения с LLM (повторяем), а не бизнес-ошибка.

    APIStatusError (4xx/5xx с телом), MaxTurnsExceeded и провал валидации сюда
    НЕ попадают — их отличаем по типу и пробрасываем как failure-статус.
    """
    if isinstance(e, (httpx.ReadError, httpx.RemoteProtocolError,
                      httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError)):
        return True
    if isinstance(e, (APIConnectionError, APITimeoutError)):
        return True
    # «Голый» APIError при стриминге (HTTP/2 RST_STREAM / INTERNAL_ERROR).
    if isinstance(e, APIError) and not isinstance(e, APIStatusError):
        return True
    return False


def _serialize_event(ev: Any) -> dict[str, Any] | None:
    """Высокоуровневое событие -> JSON-словарь. raw_response_event пропускаем."""
    if ev.type == "agent_updated_stream_event":
        return {"type": ev.type, "new_agent": ev.new_agent.name}
    if ev.type == "run_item_stream_event":
        return {
            "type": ev.type,
            "name": ev.name,
            "item_type": ev.item.type,
            "payload": ev.item.to_input_item(),
        }
    return None


async def run_streamed_and_persist(
    agent: Any,
    input: Any,
    session: SessionABC,
    pool: asyncpg.Pool,
    run_id: int,
    turn_idx: int,
    *,
    context: Any = None,
    max_turns: int | None = None,
    preamble: str | None = None,
) -> StructuredResult:
    last_exc: Exception | None = None
    for attempt in range(1, TURN_NETWORK_RETRIES + 2):
        events: list[tuple[int, int, int, dict[str, Any]]] = []
        try:
            kwargs: dict[str, Any] = {"session": session}
            if context is not None:
                kwargs["context"] = context
            if max_turns is not None:
                kwargs["max_turns"] = max_turns

            streamed = Runner.run_streamed(agent, input=_with_preamble(preamble, input), **kwargs)
            seq = 0
            async for ev in streamed.stream_events():
                serialized = _serialize_event(ev)
                if serialized is None:
                    continue
                events.append((run_id, turn_idx, seq, serialized))
                seq += 1

            result = _parse_structured_result(streamed.final_output)

            # Стрим завершён успешно — фиксируем события одним батчем.
            if events:
                await pool.executemany(
                    "INSERT INTO agent_stream_events (run_id, turn_idx, seq, event) "
                    "VALUES ($1, $2, $3, $4)",
                    events,
                )
            return result
        except Exception as e:  # noqa: BLE001
            if not is_transient(e):
                raise
            last_exc = e
            log(f"  [turn {turn_idx}] transport error (attempt {attempt}): {type(e).__name__}: {e}")
    assert last_exc is not None
    raise last_exc
