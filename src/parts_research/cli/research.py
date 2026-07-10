"""Agent-facing entry point Этапа 2: submit-and-wait.

    python -m parts_research.cli.research ARTICLE [ARTICLE ...]

Кладёт артикулы в общую очередь воркеров (атомарно, с дедупом активных ранов),
ждёт их обработки воркером(ами) и печатает результат(ы) как JSON-массив в stdout —
чтобы вызывающий (в т.ч. агент) мог прочитать результат. Логи/прогресс — в stderr.

Никаких файлов на диске. Если живого воркера нет — задачи всё равно остаются в
очереди (обработаются при запуске воркера), а команда сразу возвращает
worker_alive=false и НЕ виснет. Exit-код вторичен: всё, что важно, — в JSON.

См. PARTS_RESEARCH_SPEC.md «Постановка задач и возврат результата»."""

from __future__ import annotations

import asyncio
import json
import sys

from ..db.pool import create_pool
from ..db.tasks import TERMINAL_STATUSES, count_live_workers, submit_article
from ..research.profiles import resolve_profile
from ..research.validation import pre_validate_article

POLL_SECONDS = 1.0

# submit-guard: артикул уже финализирован человеком в Smart → ресерч бессмысленен.
_GUARD_SQL = "SELECT 1 FROM smart.parts WHERE $1 = ANY(articles) AND is_draft = false"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _apply_status(entry: dict, row, *, worker_alive: bool) -> None:
    """Заполняет статус/результат записи из строки task_runs."""
    status = row["status"]
    entry["status"] = status
    entry["worker_alive"] = worker_alive
    entry["result_json"] = row["result_json"]
    # Для needs_human_review причина лежит в task_runs.error — разносим явно.
    if status == "needs_human_review":
        entry["needs_review_reason"] = row["error"]
        entry["error"] = None
    else:
        entry["needs_review_reason"] = None
        entry["error"] = row["error"]


async def _amain(articles: list[str]) -> None:
    # Короткоживущий клиент: маленький пул, чтобы не платить ~9s за прогрев 10
    # соединений к удалённой БД (большой пул нужен только воркеру).
    pool = await create_pool(min_size=1, max_size=4)
    try:
        entries: list[dict] = []
        run_to_entries: dict[int, list[dict]] = {}

        # 1) Постановка в очередь (по каждому артикулу независимо).
        for raw in articles:
            entry: dict = {}
            entries.append(entry)
            try:
                article = pre_validate_article(raw)
            except ValueError as e:
                entry.update(
                    article=raw, task_id=None, run_id=None, reused=False,
                    status="invalid", error=str(e),
                    needs_review_reason=None, worker_alive=None, result_json=None,
                )
                continue

            finalized = await pool.fetchval(_GUARD_SQL, article)
            if finalized:
                entry.update(
                    article=article, task_id=None, run_id=None, reused=False,
                    status="refused",
                    error="already finalized in Smart (is_draft=false); research skipped",
                    needs_review_reason=None, worker_alive=None, result_json=None,
                )
                continue

            sub = await submit_article(pool, article, resolve_profile(None))
            entry.update(
                article=article, task_id=sub["task_id"], run_id=sub["run_id"],
                reused=sub["reused"],
            )
            run_to_entries.setdefault(sub["run_id"], []).append(entry)
            log(
                f"[queued] {article} task={sub['task_id']} run={sub['run_id']} "
                f"reused={sub['reused']}"
            )

        # 2) Ожидание результата(ов).
        if run_to_entries:
            if await count_live_workers(pool) == 0:
                log("[no live worker] задачи в очереди — обработаются при запуске воркера")
                for run_id, es in run_to_entries.items():
                    row = await pool.fetchrow(
                        "SELECT status, error, result_json FROM task_runs WHERE id = $1",
                        run_id,
                    )
                    for e in es:
                        _apply_status(e, row, worker_alive=False)
            else:
                pending = set(run_to_entries)
                while pending:
                    rows = await pool.fetch(
                        "SELECT id, status, error, result_json FROM task_runs "
                        "WHERE id = ANY($1::bigint[])",
                        list(pending),
                    )
                    for row in rows:
                        if row["status"] in TERMINAL_STATUSES:
                            for e in run_to_entries[row["id"]]:
                                _apply_status(e, row, worker_alive=True)
                            pending.discard(row["id"])
                    if not pending:
                        break
                    if await count_live_workers(pool) == 0:
                        log("[worker disappeared] живые воркеры исчезли во время ожидания")
                        for run_id in pending:
                            row = await pool.fetchrow(
                                "SELECT status, error, result_json FROM task_runs WHERE id = $1",
                                run_id,
                            )
                            for e in run_to_entries[run_id]:
                                _apply_status(e, row, worker_alive=False)
                        break
                    await asyncio.sleep(POLL_SECONDS)

        print(json.dumps(entries, ensure_ascii=False, indent=2))
    finally:
        await pool.close()


def main() -> None:
    articles = sys.argv[1:]
    if not articles:
        print(
            "usage: python -m parts_research.cli.research ARTICLE [ARTICLE ...]",
            file=sys.stderr,
        )
        raise SystemExit(2)
    asyncio.run(_amain(articles))


if __name__ == "__main__":
    main()
