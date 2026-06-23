"""DB-helpers очереди Этапа 2.

Всё вокруг task_runs как очереди: атомарная постановка (submit) с дедупом
активных ранов, dequeue через FOR UPDATE SKIP LOCKED, восстановление зависших
ранов и liveness воркеров через shared advisory-lock.

Принцип проекта — ошибки не скрываем: исключения летят наверх, маскирующих
фолбеков нет. Статусы run'а считаем терминальными по TERMINAL_STATUSES."""

from __future__ import annotations

import asyncpg

# Фиксированный ключ shared advisory-lock — общий liveness-сигнал всех воркеров.
# Каждый воркер держит его на отдельном соединении весь свой lifetime; cli.research
# читает pg_locks по этому ключу, чтобы понять, есть ли живой воркер.
WORKER_LIVENESS_KEY = 8273461

ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = (
    "done",
    "needs_human_review",
    "failed_no_data",
    "failed_validation",
    "failed_crashed",
    "skipped_smart_approved",
)


# ── постановка в очередь ─────────────────────────────────────────────────────────
async def submit_article(pool: asyncpg.Pool, article: str) -> dict:
    """Атомарно ставит артикул в общую очередь.

    Per-article transaction-level advisory-lock (`pg_advisory_xact_lock(hashtext)`)
    сериализует параллельные submit одного и того же артикула, поэтому дедуп
    работает без гонки. Модель «один task на артикул»:

    - find-or-create `task` по артикулу;
    - если есть активный (`queued`/`running`) run — переиспользуем его (reused=True);
    - иначе создаём новый `queued` run под этим task.

    Возвращает {"task_id", "run_id", "reused"}.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", article)

            task_id = await conn.fetchval(
                "SELECT id FROM tasks WHERE article = $1 ORDER BY id LIMIT 1", article
            )
            if task_id is None:
                task_id = await conn.fetchval(
                    "INSERT INTO tasks (article) VALUES ($1) RETURNING id", article
                )

            active = await conn.fetchval(
                "SELECT id FROM task_runs WHERE task_id = $1 "
                "AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1",
                task_id,
            )
            if active is not None:
                return {"task_id": task_id, "run_id": active, "reused": True}

            run_id = await conn.fetchval(
                "INSERT INTO task_runs (task_id, status) VALUES ($1, 'queued') RETURNING id",
                task_id,
            )
            return {"task_id": task_id, "run_id": run_id, "reused": False}


# ── dequeue воркером ──────────────────────────────────────────────────────────────
async def pick_next_queued_run(pool: asyncpg.Pool) -> asyncpg.Record | None:
    """Атомарно берёт следующий `queued` run и помечает `running` + started_at.

    `FOR UPDATE SKIP LOCKED` гарантирует, что параллельные воркеры берут разные
    раны. None — очередь пуста.
    """
    return await pool.fetchrow(
        "UPDATE task_runs SET status = 'running', started_at = now() "
        "WHERE id = ("
        "  SELECT id FROM task_runs WHERE status = 'queued' "
        "  ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
        ") "
        "RETURNING id AS run_id, task_id, "
        "(SELECT article FROM tasks WHERE id = task_runs.task_id) AS article"
    )


async def mark_crashed_stale_runs(pool: asyncpg.Pool, stale_minutes: int) -> int:
    """`running` старше stale_minutes → `failed_crashed`.

    Запускается воркером на старте: восстановление после краша процесса (раны,
    которые остались `running`, но их воркер умер). Возвращает число помеченных.
    """
    tag = await pool.execute(
        "UPDATE task_runs SET status = 'failed_crashed', "
        "error = 'worker crashed: stale running run recovered on worker startup', "
        "finished_at = now() "
        "WHERE status = 'running' AND started_at < now() - ($1 * interval '1 minute')",
        stale_minutes,
    )
    # asyncpg возвращает командный тег вида 'UPDATE N'.
    return int(tag.split()[-1])


# ── liveness воркеров ────────────────────────────────────────────────────────────
async def acquire_worker_lock(conn: asyncpg.Connection) -> None:
    """Берёт shared advisory-lock на отдельном соединении.

    Лок держится весь lifetime воркера и освобождается автоматически при
    закрытии соединения / падении процесса.
    """
    await conn.execute("SELECT pg_advisory_lock_shared($1)", WORKER_LIVENESS_KEY)


async def count_live_workers(pool: asyncpg.Pool) -> int:
    """Сколько живых воркеров (держателей shared advisory-lock) прямо сейчас."""
    return await pool.fetchval(
        "SELECT count(*) FROM pg_locks "
        "WHERE locktype = 'advisory' AND classid = 0 AND objid = $1 AND mode = 'ShareLock'",
        WORKER_LIVENESS_KEY,
    )
