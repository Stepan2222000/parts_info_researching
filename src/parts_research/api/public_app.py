"""Публичный HTTP-API для внешних систем (на других серверах): постановка
артикулов в общую очередь ресерча и получение результата — синхронно
(submit-and-wait) либо прогрессивно (submit + поллинг турнов с курсором).

Отдельное приложение/процесс, НАМЕРЕННО без эндпоинтов куратора и UI-выборок:
наружу выставляется только ресерч (куратор умеет SQL и запись в Smart — его
наружу не пускаем). Аутентификации нет — по требованию (обращаться может кто
угодно из доступной сети).

Контракт машиночитаемый: на каждый артикул возвращается status + result_json /
дельты по турнам. Семантика профилей и reuse:
  - профиль этапов фиксируется на ране; активный ран артикула переиспользуется
    (covers_requested показывает, покрывает ли его профиль запрошенный);
  - готовый done-ран с покрывающим профилем отдаётся сразу (is_final=true),
    новый ран НЕ ставится; принудительный пере-ресерч — force=true;
  - force при активном ране НЕ ставит параллельный ран (инвариант «один активный
    ран на артикул») — честно возвращается активный + force_ignored.

Принцип проекта — ошибки не скрываем: исключения летят наверх как HTTP-ошибки с
текстом, маскирующих фолбеков нет."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..auto_publish import auto_publish_run
from ..db.pool import create_pool
from ..db.tasks import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    count_live_workers,
    latest_run_for_article,
    submit_article,
)
from ..research.profiles import covers, resolve_profile
from ..research.validation import pre_validate_article
from .progress import load_progress, load_turns_payload

# Потолок синхронного ожидания одного запроса (сек). 20 минут по умолчанию
# (полный ран с phase2 занимает ~10 мин, дефолтный — меньше); переопределяется
# PARTS_RESEARCH_WAIT_TIMEOUT. По достижении — отдаём run_id + текущий статус,
# клиент дозапрашивает GET /research/{run_id} или поллит /turns.
WAIT_TIMEOUT_SECONDS = float(os.environ.get("PARTS_RESEARCH_WAIT_TIMEOUT", "1200"))
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
    # Пресет ('fast'|'default'|'full') либо {"stages": [...]}; None = default.
    profile: str | dict | None = None
    # Авто-публикация в smart сразу после ресёрча, без куратора (только однозначный
    # «зелёный коридор», см. auto_publish.py; остальное остаётся куратору, причина —
    # в auto_publish_outcome). Верхнеуровневый флаг; None = env-дефолт / ключ профиля.
    auto_publish: bool | None = None
    # Принудительный пере-ресерч, если последний ран терминален. Активный ран
    # force НЕ дублирует (см. модульный docstring).
    force: bool = False
    # wait=false в POST /research — только поставить и сразу вернуть run_id (без
    # ожидания); для клиентов за NAT/файрволами, режущими молчащие соединения.
    # Эквивалент POST /research/submit (там wait игнорируется).
    wait: bool = True


def _resolve_profile_or_400(raw: str | dict | None, auto_publish: bool | None = None) -> dict:
    """resolve_profile + верхнеуровневый флаг auto_publish (перекрывает ключ профиля)."""
    try:
        if auto_publish is not None:
            if raw is None:
                raw = {"auto_publish": auto_publish}
            elif isinstance(raw, str):
                raw = {"preset": raw, "auto_publish": auto_publish}
            elif isinstance(raw, dict):
                raw = {**raw, "auto_publish": auto_publish}
        return resolve_profile(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _base_entry(article: str) -> dict:
    return {
        "article": article, "task_id": None, "run_id": None, "reused": False,
        "profile": None, "covers_requested": None, "force_ignored": False,
        "status": None, "is_final": False, "result_json": None, "error": None,
        "needs_review_reason": None, "worker_alive": None, "timed_out": False,
        "auto_publish_outcome": None, "publication": None,
    }


def _apply_row(entry: dict, row, *, worker_alive: bool, timed_out: bool = False) -> None:
    """Заполняет статус/результат записи из строки task_runs (status/error/result_json)."""
    status = row["status"]
    entry["status"] = status
    entry["is_final"] = status in TERMINAL_STATUSES
    entry["result_json"] = row["result_json"]
    entry["worker_alive"] = worker_alive
    entry["timed_out"] = timed_out
    if "auto_publish_outcome" in dict(row):
        entry["auto_publish_outcome"] = row["auto_publish_outcome"]
    # Для needs_human_review причина лежит в task_runs.error — разносим явно.
    if status == "needs_human_review":
        entry["needs_review_reason"] = row["error"]
        entry["error"] = None
    else:
        entry["needs_review_reason"] = None
        entry["error"] = row["error"]


async def _attach_publication(pool, entry: dict) -> None:
    """Последняя публикация ЗАДАЧИ этого рана (куратором либо авто) — {smart_id,
    published_by, published_at} | None. Уровень задачи, не рана: публикация могла
    быть со старого рана, для потребителя важно «куда легла деталь в smart»."""
    if entry.get("run_id") is None:
        return
    row = await pool.fetchrow(
        "SELECT p.smart_id, p.published_by, p.published_at FROM publications p "
        "JOIN task_runs r ON r.id = p.run_id "
        "WHERE r.task_id = (SELECT task_id FROM task_runs WHERE id = $1) "
        "ORDER BY p.id DESC LIMIT 1", entry["run_id"])
    if row is not None:
        entry["publication"] = {
            "smart_id": row["smart_id"], "published_by": row["published_by"],
            "published_at": row["published_at"].isoformat(),
        }


async def _task_published(pool, task_id: int) -> bool:
    return bool(await pool.fetchval(
        "SELECT 1 FROM publications p JOIN task_runs r ON r.id = p.run_id "
        "WHERE r.task_id = $1 LIMIT 1", task_id))


async def _submit_entry(pool, raw_article: str, requested_profile: dict, force: bool) -> dict:
    """Решение по одному артикулу: invalid / refused (+ last_run) / готовый done-ран
    (is_final) / reuse активного / новый ран. Общая логика sync- и submit-эндпоинтов."""
    entry = _base_entry(raw_article)
    try:
        article = pre_validate_article(raw_article)
    except ValueError as e:
        entry.update(status="invalid", error=str(e))
        return entry
    entry["article"] = article

    finalized = await pool.fetchval(_GUARD_SQL, article)
    if finalized:
        entry.update(
            status="refused",
            error="already finalized in Smart (is_draft=false); research skipped",
        )
        # Прикладываем последний ран, если был: отказ — не тупик для потребителя.
        latest = await latest_run_for_article(pool, article)
        if latest is not None:
            entry["last_run"] = {
                "run_id": latest["run_id"], "status": latest["status"],
                "profile": latest["profile"], "result_json": latest["result_json"],
                "error": latest["error"],
            }
        return entry

    latest = await latest_run_for_article(pool, article)

    # Активный ран: переиспользуем всегда (параллельный ран того же артикула не
    # ставим даже при force — это молча удвоило бы траты LLM).
    if latest is not None and latest["status"] in ACTIVE_STATUSES:
        entry.update(
            task_id=latest["task_id"], run_id=latest["run_id"], reused=True,
            profile=latest["profile"], status=latest["status"],
            covers_requested=covers(latest["profile"], requested_profile),
            force_ignored=force,
        )
        return entry

    # Готовый done-ран с покрывающим профилем: отдаём сразу, без нового рана.
    if (
        not force
        and latest is not None
        and latest["status"] == "done"
        and covers(latest["profile"], requested_profile)
    ):
        entry.update(
            task_id=latest["task_id"], run_id=latest["run_id"], reused=True,
            profile=latest["profile"], status="done", is_final=True,
            covers_requested=True, result_json=latest["result_json"],
        )
        # Флаг auto_publish действует и при reuse: неопубликованный done-ран
        # публикуется прямо здесь (детерминированно, без LLM — быстро). Уже
        # опубликованную задачу не трогаем — publication и так вернётся ниже.
        if requested_profile["auto_publish"] and not await _task_published(pool, latest["task_id"]):
            entry["auto_publish_outcome"] = await auto_publish_run(pool, latest["run_id"], article)
        else:
            entry["auto_publish_outcome"] = await pool.fetchval(
                "SELECT auto_publish_outcome FROM task_runs WHERE id = $1", latest["run_id"])
        await _attach_publication(pool, entry)
        return entry

    sub = await submit_article(pool, article, requested_profile)
    entry.update(
        task_id=sub["task_id"], run_id=sub["run_id"], reused=sub["reused"],
        profile=sub["profile"], status="queued",
        # При гонке submit мог вернуть чужой активный ран — покрытие считаем по факту.
        covers_requested=covers(sub["profile"], requested_profile),
    )
    return entry


@app.get("/health")
async def health() -> dict:
    """Liveness для внешнего процесса: жив ли API, есть ли живой воркер, глубина очереди."""
    pool = app.state.pool
    live = await count_live_workers(pool)
    queued = await pool.fetchval("SELECT count(*) FROM task_runs WHERE status = 'queued'")
    return {"ok": True, "worker_alive": live > 0, "live_workers": live, "queued": queued}


@app.get("/research/by-article/{article}")
async def get_by_article(article: str) -> dict:
    """Последний ран артикула — «вернуться к задаче», не зная run_id.
    Дальше потребитель работает через /research/{run_id}/turns."""
    pool = app.state.pool
    try:
        normalized = pre_validate_article(article)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    latest = await latest_run_for_article(pool, normalized)
    if latest is None:
        raise HTTPException(status_code=404, detail=f"no runs for article {normalized!r}")
    return {
        "article": latest["article"],
        "task_id": latest["task_id"],
        "run_id": latest["run_id"],
        "status": latest["status"],
        "is_final": latest["status"] in TERMINAL_STATUSES,
        "profile": latest["profile"],
        "error": latest["error"],
    }


@app.get("/research/{run_id}")
async def get_research(run_id: int) -> dict:
    """Статус/результат по run_id + профиль, исходы этапов и прогресс.
    result_json у неготового рана — ПРОМЕЖУТОЧНЫЙ (растёт по турнам)."""
    pool = app.state.pool
    row = await pool.fetchrow(
        "SELECT r.id, r.status::text AS status, r.error, r.result_json, r.profile, "
        "r.stage_outcomes, r.auto_publish_outcome, t.article, r.task_id "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id WHERE r.id = $1",
        run_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    live = await count_live_workers(pool)
    entry: dict = {"article": row["article"], "task_id": row["task_id"], "run_id": run_id,
                   "auto_publish_outcome": None, "publication": None}
    _apply_row(entry, row, worker_alive=live > 0)
    entry["profile"] = row["profile"]
    entry["stage_outcomes"] = row["stage_outcomes"]
    entry["progress"] = await load_progress(pool, run_id, status=row["status"])
    await _attach_publication(pool, entry)
    return entry


@app.get("/research/{run_id}/turns")
async def get_research_turns(run_id: int, since: int = 0, mode: str = "delta") -> dict:
    """Прогрессивная выдача: новые турны после курсора `since` + дельта/снапшот.

    mode=delta (дефолт) — только изменившиеся секции JSON от состояния на турне
    `since` к текущему (слитая дельта) + русская сводка. mode=snapshot — полный
    текущий снапшот. Курсор следующего запроса = latest_turn из ответа.
    """
    pool = app.state.pool
    try:
        payload = await load_turns_payload(pool, run_id, since=since, mode=mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if payload is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return payload


@app.post("/research/submit")
async def post_research_submit(body: ResearchBody) -> dict:
    """Fire-and-forget постановка с профилем: мгновенный ответ по каждому артикулу.

    Дальше потребитель поллит GET /research/{run_id}/turns?since=<cursor>.
    Семантика reuse/force — см. модульный docstring. Невалидный артикул не валит
    остальных (status=invalid у конкретной записи)."""
    pool = app.state.pool
    requested = _resolve_profile_or_400(body.profile, body.auto_publish)
    entries = [await _submit_entry(pool, raw, requested, body.force) for raw in body.articles]
    worker_alive = await count_live_workers(pool) > 0
    for e in entries:
        e["worker_alive"] = worker_alive
    return {"worker_alive": worker_alive, "requested_profile": requested, "results": entries}


@app.post("/research")
async def post_research(body: ResearchBody) -> dict:
    """Ставит артикулы в очередь и СИНХРОННО ждёт результат (до WAIT_TIMEOUT_SECONDS).

    wait=false в теле — не ждать: сразу вернуть run_id и текущий статус
    (queued/running), результат дозапрашивается GET /research/{run_id} или
    поллингом /turns. Для клиентов, которым нельзя держать долгие соединения.

    Возвращает {worker_alive, requested_profile, results:[entry, ...]}.
    status: invalid | refused | queued | running | done | needs_human_review |
            failed_no_data | failed_validation | failed_crashed | skipped_smart_approved.
    Готовый done-ран с покрывающим профилем возвращается сразу (reused, is_final);
    force=true запускает пере-ресерч. Нет живого воркера на старте → не виснем,
    сразу отдаём queued + worker_alive=false.
    """
    pool = app.state.pool
    requested = _resolve_profile_or_400(body.profile, body.auto_publish)

    entries: list[dict] = []
    run_to_entries: dict[int, list[dict]] = {}

    # 1) Постановка в очередь (каждый артикул независимо; невалидный не валит остальных).
    for raw in body.articles:
        entry = await _submit_entry(pool, raw, requested, body.force)
        entries.append(entry)
        # Ждать нужно только раны, которые ещё не терминальны.
        if entry["run_id"] is not None and not entry["is_final"]:
            run_to_entries.setdefault(entry["run_id"], []).append(entry)

    worker_alive = await count_live_workers(pool) > 0

    # 2) Ожидание результата(ов).
    if run_to_entries:
        if not body.wait or not worker_alive:
            # wait=false (клиент дозапрашивает GET /research/{run_id}) либо
            # обрабатывать некому — отдаём текущий статус сразу, не виснем.
            for run_id, es in run_to_entries.items():
                row = await pool.fetchrow(
                    "SELECT status, error, result_json, auto_publish_outcome FROM task_runs WHERE id = $1", run_id)
                for e in es:
                    _apply_row(e, row, worker_alive=worker_alive)
        else:
            pending = set(run_to_entries)
            deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
            while pending:
                rows = await pool.fetch(
                    "SELECT id, status, error, result_json, auto_publish_outcome FROM task_runs WHERE id = ANY($1::bigint[])",
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
                            "SELECT status, error, result_json, auto_publish_outcome FROM task_runs WHERE id = $1", run_id)
                        for e in run_to_entries[run_id]:
                            _apply_row(e, row, worker_alive=False)
                    break
                if time.monotonic() >= deadline:
                    for run_id in pending:
                        row = await pool.fetchrow(
                            "SELECT status, error, result_json, auto_publish_outcome FROM task_runs WHERE id = $1", run_id)
                        for e in run_to_entries[run_id]:
                            _apply_row(e, row, worker_alive=True, timed_out=True)
                    break
                await asyncio.sleep(POLL_SECONDS)

    # Публикация терминальных записей (куратором либо авто) — в ответ; reuse-ветка
    # _submit_entry уже приложила свою, не перезапрашиваем.
    for e in entries:
        if e["is_final"] and e["publication"] is None:
            await _attach_publication(pool, e)

    return {"worker_alive": worker_alive, "requested_profile": requested, "results": entries}
