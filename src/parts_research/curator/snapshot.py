"""Snapshot очереди для куратора. Подмешивается в начало каждого user-сообщения,
чтобы куратор всегда видел актуальное состояние: счётчики по статусам + последние N
`done`-ранов без публикации (которые обычно и просят «обработать»)."""

from __future__ import annotations

import asyncpg

PENDING_LIMIT = 20


# Считаем по ПОСЛЕДНЕМУ run каждой задачи — консистентно с дашбордом (api/queries.py),
# чтобы число «ждёт куратора» в снапшоте совпадало с чипом в UI (re-run не двоит).
_LATEST = "SELECT DISTINCT ON (task_id) id AS run_id, task_id, status FROM task_runs ORDER BY task_id, id DESC"


async def load_snapshot(pool: asyncpg.Pool) -> dict:
    counts = await pool.fetch(f"SELECT status, count(*) AS n FROM ({_LATEST}) s GROUP BY status")
    pending = await pool.fetch(
        f"SELECT s.run_id, t.article, dp.name, dp.vehicle_classes, dp.is_kit "
        f"FROM ({_LATEST}) s "
        f"JOIN tasks t ON t.id = s.task_id "
        f"LEFT JOIN draft_parts dp ON dp.run_id = s.run_id "
        f"WHERE s.status = 'done' AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.run_id = s.run_id) "
        f"ORDER BY s.run_id LIMIT $1",
        PENDING_LIMIT,
    )
    return {
        "counts": {r["status"]: r["n"] for r in counts},
        "pending": [dict(r) for r in pending],
    }


def format_snapshot(snap: dict) -> str:
    counts = snap["counts"]
    pending = snap["pending"]
    lines = ["<queue>"]
    lines.append("statuses: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "—"))
    suffix = f" (показаны первые {PENDING_LIMIT})" if len(pending) == PENDING_LIMIT else ""
    lines.append(f"done без публикации: {len(pending)}{suffix}")
    for p in pending:
        classes = ",".join(p["vehicle_classes"] or []) or "—"
        lines.append(
            f"  run={p['run_id']} {p['article']} | {p['name']} | classes={classes} | kit={p['is_kit']}"
        )
    lines.append("</queue>")
    return "\n".join(lines)
