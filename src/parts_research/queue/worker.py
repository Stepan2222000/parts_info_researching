"""Воркер-цикл Этапа 2.

Долгоживущий процесс: общий пул, dequeue через SKIP LOCKED, лимит конкуренции
WORKER_CONCURRENCY, graceful drain на SIGINT/SIGTERM (inflight дорабатывают, не
отменяются), liveness через shared advisory-lock и stale-recovery на старте.
Несколько процессов воркера делят одну очередь.

Liveness-соединение держится на отдельном standalone-коннекте и пингуется
keepalive'ом раз в KEEPALIVE_SECONDS — чтобы idle-таймаут линка/NAT не уронил
advisory-lock; при разрыве соединение переподключается и lock берётся заново.

execute_run сам ставит финальный статус и пишет ошибку в task_runs.error; здесь
ничего не скрываем — обёртка только логирует и не валит цикл."""

from __future__ import annotations

import asyncio
import signal
import sys

import asyncpg

from ..config import settings
from ..db.pool import create_pool
from ..db.tasks import acquire_worker_lock, mark_crashed_stale_runs, pick_next_queued_run
from ..research.run import execute_run

# Пустая очередь → как часто перепроверять (сек). Прерывается сигналом мгновенно.
IDLE_POLL_SECONDS = 1.0
# Пинг liveness-соединения, чтобы idle-таймаут линка не уронил advisory-lock.
KEEPALIVE_SECONDS = 30.0


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def _liveness_keepalive(holder: dict, stop: asyncio.Event) -> None:
    """Держит lock-соединение «тёплым» и самовосстанавливает advisory-lock.

    holder["conn"] — текущее standalone-соединение, удерживающее shared
    advisory-lock. Раз в KEEPALIVE_SECONDS пингуем его `SELECT 1`; если оно
    умерло (idle-drop, рестарт сети) — переподключаемся и заново берём lock.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=KEEPALIVE_SECONDS)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            await holder["conn"].execute("SELECT 1")
        except Exception as e:  # noqa: BLE001 — соединение умерло, поднимаем заново
            log(f"[worker] liveness conn lost ({type(e).__name__}); reconnecting...")
            try:
                await holder["conn"].close()
            except Exception:  # noqa: BLE001
                pass
            conn = await asyncpg.connect(settings.database_url)
            await acquire_worker_lock(conn)
            holder["conn"] = conn
            log("[worker] liveness lock re-acquired")


async def run_forever() -> None:
    pool = await create_pool()
    # Отдельное standalone-соединение под liveness advisory-lock — НЕ из пула,
    # чтобы не съедать слот; лок держится весь lifetime воркера.
    lock_conn = await asyncpg.connect(settings.database_url)
    await acquire_worker_lock(lock_conn)
    holder = {"conn": lock_conn}

    recovered = await mark_crashed_stale_runs(pool, settings.worker_stale_minutes)
    log(
        f"[worker] up; concurrency={settings.worker_concurrency}; "
        f"stale-recovered={recovered}"
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    keepalive = asyncio.create_task(_liveness_keepalive(holder, stop))
    inflight: set[asyncio.Task] = set()

    async def process(run_id: int, article: str) -> None:
        try:
            status = await execute_run(pool, run_id, article)
            log(f"[worker] run {run_id} ({article}) -> {status}")
        except Exception as e:  # noqa: BLE001 — execute_run обычно сам ставит статус; тут страховка-лог
            log(f"[worker] run {run_id} ({article}) wrapper error: {type(e).__name__}: {e}")

    try:
        while not stop.is_set():
            if len(inflight) >= settings.worker_concurrency:
                await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
                continue
            row = await pick_next_queued_run(pool)
            if row is None:
                # Очередь пуста — ждём сигнала или короткий poll.
                try:
                    await asyncio.wait_for(stop.wait(), timeout=IDLE_POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                continue
            task = asyncio.create_task(process(row["run_id"], row["article"]))
            inflight.add(task)
            task.add_done_callback(inflight.discard)

        # Graceful drain: новые НЕ берём, дожидаемся inflight (не отменяем).
        if inflight:
            log(f"[worker] stop signal; draining {len(inflight)} inflight run(s)...")
            await asyncio.gather(*inflight, return_exceptions=True)
    finally:
        keepalive.cancel()
        try:
            await keepalive
        except asyncio.CancelledError:
            pass
        await holder["conn"].close()  # advisory-lock освобождается
        await pool.close()
    log("[worker] stopped")
