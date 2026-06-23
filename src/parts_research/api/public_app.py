"""Публичный HTTP-API для внешних систем (на других серверах): постановка
артикулов в общую очередь ресерча и СИНХРОННОЕ ожидание результата.

Отдельное приложение/процесс, НАМЕРЕННО без эндпоинтов куратора и UI-выборок:
наружу выставляется только ресерч (куратор умеет SQL и запись в Smart — его
наружу не пускаем). Аутентификации нет — по требованию (обращаться может кто
угодно из доступной сети).

Контракт машиночитаемый: на каждый артикул возвращается status + result_json.
Логика постановки/ожидания переиспользует те же helpers, что и `cli.research`.

Принцип проекта — ошибки не скрываем: исключения летят наверх как HTTP-ошибки с
текстом, маскирующих фолбеков нет."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..db.pool import create_pool
from ..db.tasks import TERMINAL_STATUSES, count_live_workers, submit_article
from ..research.validation import pre_validate_article

# Потолок синхронного ожидания одного запроса (сек). 10 минут по умолчанию;
# переопределяется PARTS_RESEARCH_WAIT_TIMEOUT. По достижении — отдаём run_id +
# текущий статус, клиент дозапрашивает GET /research/{run_id}.
WAIT_TIMEOUT_SECONDS = float(os.environ.get("PARTS_RESEARCH_WAIT_TIMEOUT", "600"))
POLL_SECONDS = 1.5

# submit-guard: артикул уже финализирован человеком в Smart → ресерч бессмысленен.
_GUARD_SQL = "SELECT 1 FROM smart.parts WHERE $1 = ANY(articles) AND is_draft = false"


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await create_pool(min_size=2, max_size=12)
    app.state.pool = pool
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="parts_research_public_api", lifespan=lifespan)


class ResearchBody(BaseModel):
    articles: list[str]


def _apply_row(entry: dict, row, *, worker_alive: bool, timed_out: bool = False) -> None:
    """Заполняет статус/результат записи из строки task_runs (status/error/result_json)."""
    status = row["status"]
    entry["status"] = status
    entry["result_json"] = row["result_json"]
    entry["worker_alive"] = worker_alive
    entry["timed_out"] = timed_out
    # Для needs_human_review причина лежит в task_runs.error — разносим явно.
    if status == "needs_human_review":
        entry["needs_review_reason"] = row["error"]
        entry["error"] = None
    else:
        entry["needs_review_reason"] = None
        entry["error"] = row["error"]


@app.get("/health")
async def health() -> dict:
    """Liveness для внешнего процесса: жив ли API, есть ли живой воркер, глубина очереди."""
    pool = app.state.pool
    live = await count_live_workers(pool)
    queued = await pool.fetchval("SELECT count(*) FROM task_runs WHERE status = 'queued'")
    return {"ok": True, "worker_alive": live > 0, "live_workers": live, "queued": queued}


@app.get("/research/{run_id}")
async def get_research(run_id: int) -> dict:
    """Дозапрос результата по run_id (для timed_out-случая или асинхронного использования)."""
    pool = app.state.pool
    row = await pool.fetchrow(
        "SELECT r.id, r.status::text AS status, r.error, r.result_json, t.article "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id WHERE r.id = $1",
        run_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    live = await count_live_workers(pool)
    entry: dict = {"article": row["article"], "run_id": run_id}
    _apply_row(entry, row, worker_alive=live > 0)
    return entry


@app.post("/research")
async def post_research(body: ResearchBody) -> dict:
    """Ставит артикулы в очередь и СИНХРОННО ждёт результат (до WAIT_TIMEOUT_SECONDS).

    Возвращает {worker_alive, results:[entry, ...]}. Каждый entry:
      article, task_id, run_id, reused, status, result_json, error,
      needs_review_reason, worker_alive, timed_out.

    status: invalid | refused | queued | running | done | needs_human_review |
            failed_no_data | failed_validation | failed_crashed | skipped_smart_approved.
    Нет живого воркера на старте → не виснем, сразу отдаём queued + worker_alive=false.
    """
    pool = app.state.pool
    entries: list[dict] = []
    run_to_entries: dict[int, list[dict]] = {}

    # 1) Постановка в очередь (каждый артикул независимо; невалидный не валит остальных).
    for raw in body.articles:
        entry: dict = {
            "article": raw, "task_id": None, "run_id": None, "reused": False,
            "status": None, "result_json": None, "error": None,
            "needs_review_reason": None, "worker_alive": None, "timed_out": False,
        }
        entries.append(entry)
        try:
            article = pre_validate_article(raw)
        except ValueError as e:
            entry.update(status="invalid", error=str(e))
            continue
        entry["article"] = article

        finalized = await pool.fetchval(_GUARD_SQL, article)
        if finalized:
            entry.update(
                status="refused",
                error="already finalized in Smart (is_draft=false); research skipped",
            )
            continue

        sub = await submit_article(pool, article)
        entry.update(task_id=sub["task_id"], run_id=sub["run_id"], reused=sub["reused"], status="queued")
        run_to_entries.setdefault(sub["run_id"], []).append(entry)

    worker_alive = await count_live_workers(pool) > 0

    # 2) Ожидание результата(ов).
    if run_to_entries:
        if not worker_alive:
            # Обрабатывать некому — отдаём текущий статус сразу, не виснем.
            for run_id, es in run_to_entries.items():
                row = await pool.fetchrow(
                    "SELECT status, error, result_json FROM task_runs WHERE id = $1", run_id)
                for e in es:
                    _apply_row(e, row, worker_alive=False)
        else:
            pending = set(run_to_entries)
            deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
            while pending:
                rows = await pool.fetch(
                    "SELECT id, status, error, result_json FROM task_runs WHERE id = ANY($1::bigint[])",
                    list(pending))
                for row in rows:
                    if row["status"] in TERMINAL_STATUSES:
                        for e in run_to_entries[row["id"]]:
                            _apply_row(e, row, worker_alive=True)
                        pending.discard(row["id"])
                if not pending:
                    break
                if await count_live_workers(pool) == 0:
                    for run_id in pending:
                        row = await pool.fetchrow(
                            "SELECT status, error, result_json FROM task_runs WHERE id = $1", run_id)
                        for e in run_to_entries[run_id]:
                            _apply_row(e, row, worker_alive=False)
                    break
                if time.monotonic() >= deadline:
                    for run_id in pending:
                        row = await pool.fetchrow(
                            "SELECT status, error, result_json FROM task_runs WHERE id = $1", run_id)
                        for e in run_to_entries[run_id]:
                            _apply_row(e, row, worker_alive=True, timed_out=True)
                    break
                await asyncio.sleep(POLL_SECONDS)

    return {"worker_alive": worker_alive, "results": entries}
