"""FastAPI-процесс parts_research_api (Этап 4).

Один процесс для UI-бэкенда: постановка задач (fire-and-forget поверх общей
очереди воркеров), статус очереди, detail run'а и чат куратора (SSE в протоколе
AI SDK v6). Worker не трогаем — API только читает БД и крутит curator-агента.

История куратора живёт server-side в agent_history (PostgresSession); turn'ы
одной сессии сериализуются per-session asyncio.Lock'ом, разные сессии — параллельно.

Принцип проекта — без скрытых фолбеков: ошибки летят наверх как HTTP-ошибки с
текстом либо как чанк error в SSE."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager

from exa_py import AsyncExa
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import settings
from ..curator.agent_factory import build_curator_system_prompt, make_curator_agent
from ..db.pool import create_pool
from ..db.tasks import count_live_workers, submit_article
from ..research.validation import pre_validate_article
from .queries import load_queue, load_run_detail
from .sse import curator_event_stream

# submit-guard: артикул уже финализирован человеком в Smart → ресерч бессмысленен.
_GUARD_SQL = "SELECT 1 FROM smart.parts WHERE $1 = ANY(articles) AND is_draft = false"


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await create_pool(min_size=2, max_size=12)
    exa = AsyncExa(api_key=settings.exa_api_key)
    async with pool.acquire() as conn:
        brands = [r["name"] for r in await conn.fetch("SELECT name FROM smart.brands ORDER BY name")]
        types = [r["name"] for r in await conn.fetch("SELECT name FROM smart.product_types ORDER BY name")]
    agent = make_curator_agent(build_curator_system_prompt(brands, types))

    app.state.pool = pool
    app.state.exa = exa
    app.state.curator_agent = agent
    app.state.session_locks = defaultdict(asyncio.Lock)
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="parts_research_api", lifespan=lifespan)

# Dev-удобство: фронт ходит через Next route handlers (server-side), но прямые
# вызовы с localhost тоже разрешаем — внешних потребителей у API нет.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── модели запросов ──────────────────────────────────────────────────────────
class TasksBody(BaseModel):
    articles: list[str]


class CuratorMessageBody(BaseModel):
    session_id: int | None = None
    text: str


# ── health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {"ok": True}


# ── постановка задач (fire-and-forget) ─────────────────────────────────────────
@app.post("/tasks")
async def post_tasks(body: TasksBody) -> dict:
    """Кладёт артикулы в общую очередь воркеров и сразу возвращает фидбек по каждому.

    Ничего не ждёт (в отличие от cli.research) — прогресс UI читает через polling
    /queue и /runs/{id}. Невалидный/финализированный артикул не валит остальных.
    """
    pool = app.state.pool
    results: list[dict] = []
    for raw in body.articles:
        try:
            article = pre_validate_article(raw)
        except ValueError as e:
            results.append({"article": raw, "status": "invalid", "error": str(e),
                            "task_id": None, "run_id": None, "reused": False})
            continue

        finalized = await pool.fetchval(_GUARD_SQL, article)
        if finalized:
            results.append({"article": article, "status": "refused",
                            "error": "already finalized in Smart (is_draft=false); research skipped",
                            "task_id": None, "run_id": None, "reused": False})
            continue

        sub = await submit_article(pool, article)
        results.append({"article": article, "status": "queued", "error": None,
                        "task_id": sub["task_id"], "run_id": sub["run_id"], "reused": sub["reused"]})

    worker_alive = await count_live_workers(pool) > 0
    return {"worker_alive": worker_alive, "results": results}


# ── статус очереди ──────────────────────────────────────────────────────────────
@app.get("/queue")
async def get_queue() -> dict:
    pool = app.state.pool
    snapshot = await load_queue(pool)
    snapshot["worker_alive"] = await count_live_workers(pool) > 0
    return snapshot


# ── detail run'а ────────────────────────────────────────────────────────────────
@app.get("/runs/{run_id}")
async def get_run(run_id: int) -> dict:
    detail = await load_run_detail(app.state.pool, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return detail


# ── сессии куратора ───────────────────────────────────────────────────────────
@app.post("/curator/sessions")
async def create_session() -> dict:
    session_id = await app.state.pool.fetchval(
        "INSERT INTO curator_sessions (started_at) VALUES (now()) RETURNING id"
    )
    return {"session_id": session_id}


@app.get("/curator/sessions")
async def list_sessions() -> dict:
    rows = await app.state.pool.fetch(
        "SELECT s.id, s.started_at, s.ended_at, "
        "(SELECT count(*) FROM curator_messages m WHERE m.session_id = s.id) AS message_count "
        "FROM curator_sessions s ORDER BY s.id DESC LIMIT 100"
    )
    return {
        "sessions": [
            {
                "session_id": r["id"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
                "message_count": r["message_count"],
            }
            for r in rows
        ]
    }


@app.get("/curator/sessions/{session_id}")
async def get_session(session_id: int) -> dict:
    rows = await app.state.pool.fetch(
        "SELECT id, role::text AS role, content, tool_call, created_at "
        "FROM curator_messages WHERE session_id = $1 ORDER BY id",
        session_id,
    )
    return {
        "session_id": session_id,
        "messages": [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "tool_call": r["tool_call"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


# ── чат куратора (SSE, протокол AI SDK v6) ─────────────────────────────────────
@app.post("/curator/message")
async def curator_message(body: CuratorMessageBody):
    pool = app.state.pool
    session_id = body.session_id
    if session_id is None:
        session_id = await pool.fetchval(
            "INSERT INTO curator_sessions (started_at) VALUES (now()) RETURNING id"
        )

    lock: asyncio.Lock = app.state.session_locks[session_id]

    async def guarded() -> "AsyncIterator[str]":  # noqa: F821
        # Сериализуем turn'ы одной сессии: параллельный turn испортил бы историю.
        async with lock:
            async for chunk in curator_event_stream(
                agent=app.state.curator_agent,
                pool=pool,
                exa=app.state.exa,
                session_id=session_id,
                user_text=body.text,
            ):
                yield chunk

    return StreamingResponse(
        guarded(),
        media_type="text/event-stream",
        headers={
            "x-vercel-ai-ui-message-stream": "v1",
            "cache-control": "no-cache",
            "x-curator-session-id": str(session_id),
        },
    )
