"""Прогрессивная выдача результата рана: секционная дельта между пер-турновыми
снапшотами (run_turns) + русская сводка изменений + сборка payload'а для
GET /research/{run_id}/turns и блока progress.

Дельта — на уровне СЕКЦИЙ JSON (верхнеуровневые поля StructuredResult; numbers
разбит на три подсекции): отдаём только изменившиеся секции целиком, слияние у
потребителя — простая замена одноимённых секций. Удаления видны естественно
(секция пришла без элемента) и проговариваются в сводке. Данные не монотонны:
difference-turn может переклассифицировать номер confirmed -> irrelevant.

Ошибки не скрываем: неверный курсор/режим -> ValueError с текстом (эндпоинт
превращает в HTTP 400); упавшие турны отдаются в списке с текстом ошибки."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from ..db.tasks import TERMINAL_STATUSES

# Секции дельты. numbers разбит на подсекции, чтобы изменение одного bucket'а
# не пересылало весь numbers. Порядок здесь = порядок в сводке.
SCALAR_SECTIONS = ("task_part_number", "name", "name_en", "description", "description_en", "is_kit")
OBJECT_SECTIONS = ("weight", "models")
LIST_SECTIONS = ("brand_oem", "vehicle_classes", "kit_contents", "part_of_kits",
                 "us_prices", "nuances", "supersession")
NUMBER_BUCKETS = ("article", "article_low_confidence", "irrelevant")

_BUCKET_RU = {"article": "confirmed", "article_low_confidence": "под вопросом", "irrelevant": "irrelevant"}
_LIST_RU = {
    "us_prices": "цены",
    "nuances": "нюансы",
    "supersession": "порядок замен",
    "kit_contents": "состав набора",
    "part_of_kits": "входимость в наборы",
}
_FIELD_RU = {
    "name": "название", "name_en": "название EN",
    "description": "описание", "description_en": "описание EN",
    "weight": "вес", "models": "применяемость",
    "brand_oem": "бренды", "vehicle_classes": "классы техники", "is_kit": "признак набора",
    "task_part_number": "артикул задачи",
}


def sections_of(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Снапшот StructuredResult -> плоский словарь секций (numbers.* отдельно).

    Списковые секции нормализуются: отсутствие секции == пустой список — иначе
    дельта от пустого baseline ({}) помечала бы пустые списки как «изменение»."""
    out: dict[str, Any] = {}
    for k in SCALAR_SECTIONS + OBJECT_SECTIONS:
        out[k] = snapshot.get(k)
    for k in LIST_SECTIONS:
        out[k] = snapshot.get(k) or []
    numbers = snapshot.get("numbers") or {}
    for b in NUMBER_BUCKETS:
        out[f"numbers.{b}"] = numbers.get(b) or []
    return out


def _jeq(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


def section_delta(prev: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    """Изменившиеся секции: имя -> НОВОЕ значение целиком. prev={} — всё заполненное
    считается изменившимся (старт с нулевого курсора)."""
    prev_s, cur_s = sections_of(prev), sections_of(cur)
    return {k: cur_s[k] for k in cur_s if not _jeq(prev_s.get(k), cur_s[k])}


def _articles_by_bucket(snapshot: dict[str, Any]) -> dict[str, str]:
    """артикул -> bucket по всем numbers.* (для отслеживания переклассификаций)."""
    numbers = snapshot.get("numbers") or {}
    out: dict[str, str] = {}
    for bucket in NUMBER_BUCKETS:
        for item in numbers.get(bucket) or []:
            if isinstance(item, dict) and item.get("article"):
                out[item["article"]] = bucket
    return out


def summarize_delta(prev: dict[str, Any], cur: dict[str, Any], changed: dict[str, Any]) -> str:
    """Русская сводка изменений: номера — поимённо (добавлен/удалён/переехал между
    bucket'ами), списки — счётчиками, скаляры/объекты — «заполнено/обновлено»."""
    parts: list[str] = []

    prev_arts, cur_arts = _articles_by_bucket(prev), _articles_by_bucket(cur)
    added_by_bucket: dict[str, list[str]] = {}
    for art, bucket in cur_arts.items():
        if art not in prev_arts:
            added_by_bucket.setdefault(bucket, []).append(art)
    for bucket in NUMBER_BUCKETS:
        arts = sorted(added_by_bucket.get(bucket, []))
        if arts:
            shown = ", ".join(arts[:4]) + ("…" if len(arts) > 4 else "")
            parts.append(f"+{len(arts)} в {_BUCKET_RU[bucket]} ({shown})")
    for art, prev_bucket in prev_arts.items():
        cur_bucket = cur_arts.get(art)
        if cur_bucket is None:
            parts.append(f"{art} удалён из {_BUCKET_RU[prev_bucket]}")
        elif cur_bucket != prev_bucket:
            parts.append(f"{art}: {_BUCKET_RU[prev_bucket]} → {_BUCKET_RU[cur_bucket]}")

    for field, label in _LIST_RU.items():
        if field not in changed:
            continue
        was, now = len(prev.get(field) or []), len(cur.get(field) or [])
        if now > was:
            parts.append(f"+{now - was} {label} (всего {now})")
        elif now < was:
            parts.append(f"−{was - now} {label} (всего {now})")
        else:
            parts.append(f"обновлены {label}")

    for field in SCALAR_SECTIONS + OBJECT_SECTIONS + ("brand_oem", "vehicle_classes"):
        if field not in changed or field not in _FIELD_RU:
            continue
        old = prev.get(field)
        verb = "заполнено" if old in (None, [], "", {}) else "обновлено"
        parts.append(f"{verb}: {_FIELD_RU[field]}")

    return "; ".join(parts) if parts else "без содержательных изменений"


# ── сборка payload'ов ────────────────────────────────────────────────────────────
_RUN_SQL = (
    "SELECT r.id AS run_id, r.task_id, r.status::text AS status, r.error, r.profile, "
    "r.stage_outcomes, r.result_json, r.created_at, r.started_at, r.finished_at, t.article "
    "FROM task_runs r JOIN tasks t ON t.id = r.task_id WHERE r.id = $1"
)

_TURNS_SQL = (
    "SELECT turn_idx, stage, status, started_at, finished_at, error, result_json "
    "FROM run_turns WHERE run_id = $1 ORDER BY turn_idx"
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _turn_row(t: asyncpg.Record, summary: str | None) -> dict[str, Any]:
    duration = None
    if t["started_at"] is not None and t["finished_at"] is not None:
        duration = round((t["finished_at"] - t["started_at"]).total_seconds(), 1)
    return {
        "turn_idx": t["turn_idx"],
        "stage": t["stage"],
        "status": t["status"],
        "started_at": _iso(t["started_at"]),
        "finished_at": _iso(t["finished_at"]),
        "duration_s": duration,
        "error": t["error"],
        "summary": summary,
    }


async def load_progress(pool: asyncpg.Pool, run_id: int, *, status: str) -> dict[str, Any]:
    """Блок progress: текущий этап (из running-строки run_turns), число завершённых
    турнов, позиция в очереди (только для queued)."""
    rows = await pool.fetch(
        "SELECT turn_idx, stage, status FROM run_turns WHERE run_id = $1 ORDER BY turn_idx", run_id
    )
    current_stage = next((r["stage"] for r in rows if r["status"] == "running"), None)
    turns_done = sum(1 for r in rows if r["status"] in ("ok", "failed"))
    queue_position = None
    if status == "queued":
        queue_position = await pool.fetchval(
            "SELECT count(*) FROM task_runs WHERE status = 'queued' AND id < $1", run_id
        )
    return {
        "current_stage": current_stage,
        "turns_done": turns_done,
        "queue_position": queue_position,
    }


async def load_turns_payload(
    pool: asyncpg.Pool, run_id: int, *, since: int, mode: str
) -> dict[str, Any] | None:
    """Payload GET /research/{run_id}/turns. None — ран не найден.

    mode='delta'    — изменившиеся секции от состояния на турне `since` к последнему
                      ok-снапшоту (слитая дельта; since=0 — от пустого состояния).
    mode='snapshot' — полный текущий снапшот.
    Курсор потребителя = latest_turn из ответа: макс. turn_idx ЗАВЕРШЁННЫХ турнов
    (ok И failed — чтобы упавший последним этап не отдавался бесконечно; running
    не входит — его снапшота ещё нет, курсор не должен его перепрыгивать).

    Legacy-раны (до run_turns): turns=[], latest_turn=0, снапшот — финальный
    result_json рана, дельта считается от пустого состояния к нему.
    """
    if mode not in ("delta", "snapshot"):
        raise ValueError(f"mode must be 'delta' or 'snapshot', got {mode!r}")
    if since < 0:
        raise ValueError(f"since must be >= 0, got {since}")

    run = await pool.fetchrow(_RUN_SQL, run_id)
    if run is None:
        return None
    turn_rows = await pool.fetch(_TURNS_SQL, run_id)

    legacy = not turn_rows and run["result_json"] is not None and run["status"] in TERMINAL_STATUSES
    # Курсор двигают только ЗАВЕРШЁННЫЕ турны (ok/failed). Running-турн в latest_turn
    # не входит: его снапшота ещё нет, и потребитель, взяв его idx как курсор,
    # навсегда перепрыгнул бы содержимое этого турна.
    latest_turn = max(
        (t["turn_idx"] for t in turn_rows if t["status"] in ("ok", "failed")), default=0
    )
    if since > latest_turn:
        raise ValueError(
            f"since={since} is ahead of latest_turn={latest_turn} for run {run_id}; "
            "use latest_turn from the previous response as the cursor"
        )

    # Снапшоты ok-турнов по turn_idx (для дельт и пер-турновых сводок).
    ok_snapshots: dict[int, dict] = {
        t["turn_idx"]: t["result_json"] for t in turn_rows
        if t["status"] == "ok" and t["result_json"] is not None
    }

    def snapshot_at(cursor: int) -> dict:
        """Состояние на курсоре: последний ok-снапшот с turn_idx <= cursor."""
        idxs = [i for i in ok_snapshots if i <= cursor]
        return ok_snapshots[max(idxs)] if idxs else {}

    latest_snapshot: dict | None = None
    if ok_snapshots:
        latest_snapshot = ok_snapshots[max(ok_snapshots)]
    elif legacy:
        latest_snapshot = run["result_json"]

    # Пер-турновые сводки для новых (после since) турнов — «что нового» в UI/агенте.
    new_turns: list[dict[str, Any]] = []
    for t in turn_rows:
        if t["turn_idx"] <= since:
            continue
        summary = None
        if t["status"] == "ok" and t["turn_idx"] in ok_snapshots:
            prev = snapshot_at(t["turn_idx"] - 1)
            cur = ok_snapshots[t["turn_idx"]]
            summary = summarize_delta(prev, cur, section_delta(prev, cur))
        new_turns.append(_turn_row(t, summary))

    payload: dict[str, Any] = {
        "run_id": run["run_id"],
        "task_id": run["task_id"],
        "article": run["article"],
        "status": run["status"],
        "is_final": run["status"] in TERMINAL_STATUSES,
        "error": run["error"],
        "profile": run["profile"],
        "stage_outcomes": run["stage_outcomes"],
        "progress": await load_progress(pool, run_id, status=run["status"]),
        "latest_turn": latest_turn,
        "legacy_run": legacy,
        "turns": new_turns,
        "delta": None,
        "snapshot": None,
    }

    if mode == "snapshot":
        payload["snapshot"] = latest_snapshot
    else:
        if latest_snapshot is None:
            # Ещё нет ни одного ok-снапшота (ран в начале пути / упал до main) —
            # дельты нет, это видно по latest_turn/turns; не выдумываем пустышку.
            payload["delta"] = None
        else:
            baseline = snapshot_at(since) if not legacy else {}
            changed = section_delta(baseline, latest_snapshot)
            payload["delta"] = {
                "changed": changed,
                "summary": summarize_delta(baseline, latest_snapshot, changed),
            }
    return payload
