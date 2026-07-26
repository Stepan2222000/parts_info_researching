"""Разовый бэкфилл vehicle_classes дешёвой LLM (авто-курация, шаг 0).

Майские done-раны (контракт до миграции 004) не имеют vehicle_classes, но имеют
название, бренд, описание и модели техники — классы восстановимы без web одним
дешёвым вызовом на пачку. Прогон 2026-07-26 на 15 живых ранах: 15/15 корректно
(Skandic->snowmobile, Sea-Doo->jetski, Mercedes->auto, мультикласс BRP).

Пишем ТОЛЬКО уверенные непустые ответы; след — draft_parts.classes_backfill
{model, at, confident}. Не уверена модель — ран остаётся без классов (уйдёт
LLM-куратору как no_classes). Ошибки батча показываем текстом и идём дальше.

Запуск:  python -m parts_research.cli.backfill_classes [--dry-run] [--limit N] [--batch N]
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter

import asyncpg

from ..config import settings
from ..db.pool import create_pool
from ..similar import judge_json


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


_SYS = """Ты классифицируешь запчасти для техники по классам транспорта.
Допустимые классы: {slugs}.
boat — лодки/катера (подвесные и стационарные моторы, приводы, трансы);
jetski — гидроциклы; quad — квадроциклы/ATV/SSV; snowmobile — снегоходы;
motorcycle — мотоциклы; auto — автомобили.
Деталь может подходить нескольким классам сразу — укажи все.
Решай ТОЛЬКО по данным карточки (название, бренд, модели, описание) — ничего не выдумывай.
Если по данным класс определить нельзя уверенно — верни пустой список классов и confident=false.
Ориентиры: MerCruiser/Mercury/Mariner/Quicksilver, Volvo Penta -> boat;
Sea-Doo (Spark/GTI/GTX/RXP/FishPro) -> jetski; Ski-Doo/Lynx (MXZ/Skandic/Expedition) -> snowmobile;
Can-Am (Outlander/Renegade/Maverick/Commander/DS) -> quad. Но всегда проверяй по моделям карточки."""


def _schema(slugs: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "runs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "integer"},
                        "vehicle_classes": {"type": "array",
                                            "items": {"type": "string", "enum": slugs}},
                        "confident": {"type": "boolean"},
                    },
                    "required": ["run_id", "vehicle_classes", "confident"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["runs"],
        "additionalProperties": False,
    }


async def _load_targets(pool: asyncpg.Pool, limit: int | None) -> list[asyncpg.Record]:
    sql = """
        SELECT DISTINCT ON (r.task_id) r.id AS run_id, t.article, dp.id AS dp_id,
               dp.name, dp.name_en, dp.brand_oem, dp.models_text, dp.description
        FROM task_runs r
        JOIN tasks t ON t.id = r.task_id
        JOIN draft_parts dp ON dp.run_id = r.id
        WHERE r.status = 'done'
          AND cardinality(dp.vehicle_classes) = 0
          AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.run_id = r.id)
        ORDER BY r.task_id, r.id DESC"""
    rows = await pool.fetch(sql)
    rows.sort(key=lambda r: r["run_id"])
    return rows[:limit] if limit else rows


def _card(r: asyncpg.Record) -> dict:
    return {
        "run_id": r["run_id"], "article": r["article"], "name": r["name"],
        "name_en": r["name_en"], "brands": list(r["brand_oem"] or []),
        "models": (r["models_text"] or "")[:600],
        "description": (r["description"] or "")[:400],
    }


async def _amain(dry_run: bool, limit: int | None, batch_size: int) -> None:
    pool = await create_pool(min_size=1, max_size=4)
    try:
        slugs = [r["slug"] for r in await pool.fetch(
            "SELECT slug FROM smart.vehicle_classes ORDER BY position")]
        targets = await _load_targets(pool, limit)
        log(f"[backfill] runs without classes: {len(targets)}; model={settings.llm_model_judge}; "
            f"dry_run={dry_run}")
        schema, system = _schema(slugs), _SYS.format(slugs=", ".join(slugs))
        stats: Counter = Counter()
        for i in range(0, len(targets), batch_size):
            batch = targets[i:i + batch_size]
            cards = [_card(r) for r in batch]
            try:
                out = await judge_json(system, "Карточки ранов:\n" + json.dumps(
                    cards, ensure_ascii=False), schema, "classes")
            except Exception as e:  # noqa: BLE001 — батч показываем и идём дальше
                log(f"[backfill] batch {i // batch_size}: ERROR {type(e).__name__}: {e}")
                stats["batch_error"] += len(batch)
                continue
            by_id = {r["run_id"]: r for r in batch}
            answered = set()
            for v in out["runs"]:
                r = by_id.get(v["run_id"])
                if r is None:
                    continue
                answered.add(v["run_id"])
                classes = list(dict.fromkeys(v["vehicle_classes"]))
                if not v["confident"] or not classes:
                    stats["unconfident"] += 1
                    log(f"  run {v['run_id']} {r['article']}: НЕ УВЕРЕН — пропуск")
                    continue
                if dry_run:
                    stats["would_update"] += 1
                    log(f"  run {v['run_id']} {r['article']}: [{','.join(classes)}] (dry-run)")
                    continue
                await pool.execute(
                    "UPDATE draft_parts SET vehicle_classes = $2, "
                    "classes_backfill = jsonb_build_object("
                    "  'model', $3::text, 'at', now(), 'confident', true) "
                    "WHERE id = $1",
                    r["dp_id"], classes, settings.llm_model_judge)
                stats["updated"] += 1
                log(f"  run {v['run_id']} {r['article']}: [{','.join(classes)}]")
            for rid in set(by_id) - answered:
                stats["no_answer"] += 1
                log(f"  run {rid}: модель не вернула ответ — пропуск")
        print(json.dumps(dict(stats), ensure_ascii=False))
    finally:
        await pool.close()


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    limit = None
    batch = 20
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    if "--batch" in args:
        batch = int(args[args.index("--batch") + 1])
    asyncio.run(_amain(dry_run, limit, batch))


if __name__ == "__main__":
    main()
