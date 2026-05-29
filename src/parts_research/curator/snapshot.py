"""Snapshot очереди для куратора. Подмешивается в начало каждого user-сообщения,
чтобы куратор всегда видел актуальное состояние: счётчики по статусам + последние N
`done`-ранов без публикации (которые обычно и просят «обработать»)."""

from __future__ import annotations

import asyncpg

PENDING_LIMIT = 20


async def load_snapshot(pool: asyncpg.Pool) -> dict:
    counts = await pool.fetch("SELECT status, count(*) AS n FROM task_runs GROUP BY status")
    pending = await pool.fetch(
        "SELECT r.id AS run_id, t.article, dp.name, dp.product_type, dp.is_kit "
        "FROM task_runs r "
        "JOIN tasks t ON t.id = r.task_id "
        "JOIN draft_parts dp ON dp.run_id = r.id "
        "WHERE r.status = 'done' AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.run_id = r.id) "
        "ORDER BY r.id LIMIT $1",
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
        lines.append(
            f"  run={p['run_id']} {p['article']} | {p['name']} | {p['product_type']} | kit={p['is_kit']}"
        )
    lines.append("</queue>")
    return "\n".join(lines)
