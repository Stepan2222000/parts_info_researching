"""Batch-sweep автопрохода по всей очереди (авто-курация, слой 1).

Прогоняет существующий зелёный коридор (auto_publish.auto_publish_run) по всем
неопубликованным done-ранам (последний ран каждой задачи). Группы НЕ сливает —
гейт «группа из одного» остаётся как есть, группы уходят в hard list куратору.

--dry-run: только гейты (_gates_and_payload) в откатываемой транзакции — ничего
не публикует и outcome не пишет; печатает, что БЫ произошло, и сводку по типам.

Реальный прогон идемпотентен: опубликованные раны отсеиваются гейтом
«run already published», исходы пишутся в task_runs.auto_publish_outcome, воронка
похожести (similar) отрабатывает после каждого INSERT как в обычном авто-режиме.

Запуск:  python -m parts_research.cli.curation_sweep [--dry-run] [--limit N]
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter

import asyncpg

from ..auto_publish import _Skip, _gates_and_payload, auto_publish_run, classify_skip_reason
from ..db.pool import create_pool


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def _load_pool_runs(pool: asyncpg.Pool, limit: int | None) -> list[asyncpg.Record]:
    rows = await pool.fetch("""
        SELECT DISTINCT ON (r.task_id) r.id AS run_id, t.article
        FROM task_runs r JOIN tasks t ON t.id = r.task_id
        WHERE r.status = 'done'
          AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.run_id = r.id)
        ORDER BY r.task_id, r.id DESC""")
    rows.sort(key=lambda r: r["run_id"])
    return rows[:limit] if limit else rows


async def _dry_run_one(pool: asyncpg.Pool, run_id: int) -> tuple[str, str]:
    """(исход, деталь) без каких-либо записей: гейты в откатываемой транзакции."""
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            part, _notes = await _gates_and_payload(conn, run_id)
            mode = "update" if part.smart_id else "insert"
            return f"would_publish_{mode}", part.smart_id or ""
        except _Skip as s:
            return "skipped", str(s)
        finally:
            await tr.rollback()


async def _amain(dry_run: bool, limit: int | None) -> None:
    pool = await create_pool(min_size=2, max_size=6)
    try:
        runs = await _load_pool_runs(pool, limit)
        log(f"[sweep] unpublished done runs: {len(runs)}; dry_run={dry_run}")
        stats: Counter = Counter()
        reasons: Counter = Counter()
        for i, r in enumerate(runs, 1):
            rid, article = r["run_id"], r["article"]
            if dry_run:
                outcome, detail = await _dry_run_one(pool, rid)
                stats[outcome] += 1
                if outcome == "skipped":
                    reasons[classify_skip_reason(detail)] += 1
                log(f"  [{i}/{len(runs)}] run {rid} {article}: {outcome} {detail[:120]}")
                continue
            out = await auto_publish_run(pool, rid, article)
            decision = out.get("decision", "?")
            if decision == "published":
                stats[f"published_{out.get('mode')}"] += 1
                flagged = (out.get("similar") or {}).get("flagged")
                sim = f" similar_flagged={len(flagged)}" if flagged else ""
                log(f"  [{i}/{len(runs)}] run {rid} {article}: published {out.get('mode')} "
                    f"smart={out.get('smart_id')}{sim}")
            elif decision == "skipped":
                stats["skipped"] += 1
                reasons[classify_skip_reason(out.get("reason"))] += 1
                log(f"  [{i}/{len(runs)}] run {rid} {article}: skipped {str(out.get('reason'))[:120]}")
            else:
                stats["error"] += 1
                log(f"  [{i}/{len(runs)}] run {rid} {article}: ERROR {str(out.get('error'))[:200]}")
        print(json.dumps({"outcomes": dict(stats), "skip_types": dict(reasons)},
                         ensure_ascii=False))
    finally:
        await pool.close()


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    asyncio.run(_amain(dry_run, limit))


if __name__ == "__main__":
    main()
