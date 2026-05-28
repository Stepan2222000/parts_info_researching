"""Отладочный entry point Этапа 1: один артикул через весь pipeline без worker'а.

    python -m parts_research.cli.research ARTICLE

Делает submit-guard через FDW (отказ, если артикул финализирован в Smart),
создаёт task + run, гоняет execute_run, печатает статус/ошибку/результат и
сводку по draft-таблицам. Exit-коды вторичны — ориентируйся на текст."""

from __future__ import annotations

import asyncio
import json
import sys

from ..db.pool import create_pool
from ..research.run import execute_run
from ..research.validation import pre_validate_article


async def _amain(raw_article: str) -> int:
    article = pre_validate_article(raw_article)  # ValueError при невалидном

    pool = await create_pool()
    try:
        finalized = await pool.fetchval(
            "SELECT 1 FROM smart.parts WHERE $1 = ANY(articles) AND is_draft = false",
            article,
        )
        if finalized:
            print(
                f"[refused] article {article} is already finalized in Smart "
                "(is_draft=false); research skipped"
            )
            return 0

        task_id = await pool.fetchval(
            "INSERT INTO tasks (article) VALUES ($1) RETURNING id", article
        )
        run_id = await pool.fetchval(
            "INSERT INTO task_runs (task_id, status) VALUES ($1, 'queued') RETURNING id",
            task_id,
        )
        print(f"[queued] task={task_id} run={run_id} article={article}")

        status = await execute_run(pool, run_id, article)

        row = await pool.fetchrow(
            "SELECT status, error, result_json FROM task_runs WHERE id = $1", run_id
        )
        print(f"\n=== run {run_id} -> {row['status']} ===")
        if row["error"]:
            print(f"error: {row['error']}")
        if row["result_json"]:
            print(json.dumps(row["result_json"], ensure_ascii=False, indent=2))

        # Сводка по draft-таблицам и stream-событиям.
        counts = await pool.fetchrow(
            "SELECT "
            "(SELECT count(*) FROM draft_parts WHERE run_id=$1) AS parts, "
            "(SELECT count(*) FROM draft_part_articles dpa JOIN draft_parts dp ON dp.id=dpa.draft_part_id WHERE dp.run_id=$1) AS articles, "
            "(SELECT count(*) FROM draft_kit_components dkc JOIN draft_parts dp ON dp.id=dkc.draft_part_id WHERE dp.run_id=$1) AS components, "
            "(SELECT count(*) FROM agent_history WHERE session_id='research_run_'||$1) AS history, "
            "(SELECT count(*) FROM agent_stream_events WHERE run_id=$1) AS events, "
            "(SELECT count(*) FROM exa_cache_usage WHERE run_id=$1) AS exa_calls",
            run_id,
        )
        print(
            f"\ndraft: parts={counts['parts']} articles={counts['articles']} "
            f"components={counts['components']} | history={counts['history']} "
            f"events={counts['events']} exa_calls={counts['exa_calls']}"
        )
        return 0 if row["status"] in ("done", "needs_human_review") else 1
    finally:
        await pool.close()


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m parts_research.cli.research ARTICLE", file=sys.stderr)
        raise SystemExit(2)
    try:
        code = asyncio.run(_amain(sys.argv[1]))
    except ValueError as e:
        print(f"[invalid article] {e}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
