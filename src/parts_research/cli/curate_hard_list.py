"""LLM-куратор по hard list (авто-курация, слой 3) и дедуп-очереди.

Hard list = done-раны без публикации, которым batch-sweep записал
auto_publish_outcome.decision='skipped'. Раннер собирает их в батчи ≤ N
(CURATOR_GET_CONTEXT_MAX_PARTS) ОДНОГО типа причины; группы (пересечение
confirmed-номеров) кладутся в батч целиком и никогда не рвутся. Каждый батч —
отдельная curator-сессия: тот же агент, что в REPL/UI, плюс задание-инструкция
(web-поиск обязателен, публикует сам, склейка без общего номера только с
пруф-URL, замороженные записи не трогает, не уверен -> mark_needs_review).

frozen_or_final в батчи НЕ попадает — эти раны разбирает человек.

Идемпотентность бесплатная: опубликованные и припаркованные раны выпадают из
hard list сами (publications / needs_human_review), недоделанный батч можно
перегнать повторно.

--dedup — вместо hard list разбирает открытые dedup_candidates (гипотезы
«опубликованная запись — дубль существующей» от воронки похожести).

Запуск:  python -m parts_research.cli.curate_hard_list
             [--dry-run] [--max-batches N] [--types t1,t2] [--dedup]
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict

import asyncpg
from exa_py import AsyncExa

from agents import Runner

from ..auto_publish import classify_skip_reason
from ..config import settings
from ..db.pool import create_pool
from ..db.session import PostgresSession
from ..curator.agent_factory import build_curator_system_prompt, make_curator_agent
from ..curator.repl import _load_allowed, _new_session, _persist_events
from ..curator.snapshot import format_snapshot, load_snapshot
from ..curator.tools import CuratorRunContext

# Батч куратора может требовать десятки тул-вызовов (get_context + web-поиск на
# каждую деталь + save_to_smart) — дефолтных 10 turn'ов SDK мало.
MAX_TURNS_PER_BATCH = 120

# Человеку, не LLM: замки не снимаем; already_published — техношум, не работа;
# judge_error — транзиентный сбой судьи, переоценивается следующим свипом.
_EXCLUDED_TYPES = {"frozen_or_final", "already_published", "judge_error"}

_BATCH_INSTRUCTIONS = """Режим авто-курации hard list: разбери батч ниже ПОЛНОСТЬЮ, без подтверждений человека.

Правила режима (поверх обычных правил куратора):
1. Начни с get_context по ВСЕМ артикулам батча сразу.
2. Для каждой детали/группы ОБЯЗАТЕЛЬНО проверь данные в сети (web_search_exa / web_fetch_exa), прежде чем решать. Не паркуй ран, не поискав.
3. Группы (пересечение confirmed-номеров) — реши по данным и сети: один это товар (supersession/кроссы) или разные детали. Один товар -> публикуй ОДНОЙ записью (also_run_ids), объединив номера. Разные -> публикуй раздельно; общий «мостиковый» номер оставь только той детали, которой он реально принадлежит.
4. Публикуй сам через save_to_smart. Записи с is_draft=false или с is_unverified=false составом НЕ трогай и замки НЕ снимай — такой ран пропусти с mark_needs_review('frozen: <что именно заморожено>').
5. Слияние с существующей smart-записью БЕЗ единого общего артикула разрешено ТОЛЬКО с пруфом из сети. Пруф зафиксируй: после save_to_smart вставь через execute_sql строку в dedup_candidates (publication_id возьми из publications по своему run_id) со status='merged', proof_urls=ARRAY['<url1>',...], reason='<что доказал пруф>'. Нет пруфа — оставляй раздельно: дубль дешевле ложной склейки.
5а. Тип name_similar: судья-дедуп придержал ран, потому что деталь похожа на существующую запись (она названа в причине). Проверь в сети: тот же товар -> публикуй ран В эту запись (save_to_smart со smart_id той записи; это и есть слияние — п.5 про пруф обязателен); разные товары -> публикуй новой записью.
6. Формальные блокеры: слишком длинный title -> сократи name, сохранив смысл, и публикуй; бренда нет в smart.brands -> проверь, не алиас ли это существующего Smart-бренда (brand_mapping), иначе mark_needs_review; битый формат артикула -> исправь по article_formats из get_context.
7. Не уверен в решении — mark_needs_review с внятной причиной. Ничего не выдумывай.

Батч (тип: {kind}):
{items}"""

_DEDUP_INSTRUCTIONS = """Режим дедупа каталога: ниже гипотезы «свежеопубликованная запись — дубль существующей» от воронки похожести (LLM-судья по имени/моделям; общих артикулов у пар НЕТ). Разбери каждую пару ПОЛНОСТЬЮ, без подтверждений человека.

По каждой паре:
1. Изучи обе записи (execute_sql по smart.parts/parts_en/part_brands/part_components) и ОБЯЗАТЕЛЬНО проверь в сети (web_search_exa / web_fetch_exa): один это физический товар или соседние детали.
2. Доказано, что один товар (есть пруф-URL): слей записи — перенеси недостающие артикулы/данные в старшую запись, перенеси ссылки (part_components, publications.smart_id), удали дубликат; затем UPDATE dedup_candidates SET status='merged', proof_urls=ARRAY['<url>',...], resolution_note='<что доказал пруф>', resolved_at=now() WHERE id=<id>. Записи с is_draft=false не трогай — вместо слияния оставь пару человеку (resolution_note='frozen', status='open').
3. Доказано, что разные (другой размер/шаг/применение): UPDATE ... SET status='kept_separate', resolution_note='<почему разные>', resolved_at=now().
4. Пруфа нет ни туда ни сюда: status='kept_separate', resolution_note='no proof — дубль дешевле ложной склейки'.

Пары:
{items}"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def _load_hard_list(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("""
        SELECT DISTINCT ON (r.task_id) r.id AS run_id, t.article,
               COALESCE(r.auto_publish_outcome->>'reason',
                        r.auto_publish_outcome->>'error') AS reason
        FROM task_runs r JOIN tasks t ON t.id = r.task_id
        WHERE r.status = 'done'
          AND r.auto_publish_outcome->>'decision' IN ('skipped', 'error')
          AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.run_id = r.id)
        ORDER BY r.task_id, r.id DESC""")
    return [{"run_id": r["run_id"], "article": r["article"], "reason": r["reason"],
             "kind": classify_skip_reason(r["reason"])} for r in rows]


async def _confirmed_sets(pool: asyncpg.Pool, run_ids: list[int]) -> dict[int, set[str]]:
    conf: dict[int, set[str]] = {rid: set() for rid in run_ids}
    for r in await pool.fetch(
            "SELECT dp.run_id AS rid, upper(a.article) AS art "
            "FROM draft_part_articles a JOIN draft_parts dp ON dp.id = a.draft_part_id "
            "WHERE dp.run_id = ANY($1::bigint[]) AND a.confidence = 'confirmed' "
            "AND length(a.article) BETWEEN 4 AND 20", run_ids):
        conf[r["rid"]].add(r["art"])
    return conf


def _components(conf: dict[int, set[str]]) -> list[list[int]]:
    """Связные компоненты по пересечению confirmed-номеров (группы не рвём)."""
    art2runs: dict[str, list[int]] = defaultdict(list)
    for rid, arts in conf.items():
        for a in arts:
            art2runs[a].append(rid)
    seen: set[int] = set()
    comps: list[list[int]] = []
    for rid in conf:
        if rid in seen:
            continue
        stack, comp = [rid], []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.append(x)
            for a in conf[x]:
                stack.extend(y for y in art2runs[a] if y not in seen)
        comps.append(sorted(comp))
    return comps


def _make_batches(items: list[dict], conf: dict[int, set[str]], cap: int) -> list[dict]:
    """Батчи одного типа причины; компоненты групп целиком, синглы чанками по cap."""
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_kind[it["kind"]].append(it)
    batches: list[dict] = []
    for kind in sorted(by_kind):
        kind_items = by_kind[kind]
        if kind == "group":
            by_run = {it["run_id"]: it for it in kind_items}
            units = [[by_run[rid] for rid in comp if rid in by_run]
                     for comp in _components({rid: conf.get(rid, set()) for rid in by_run})]
            units = [u for u in units if u]
        else:
            units = [[it] for it in kind_items]
        cur: list[dict] = []
        for unit in units:
            if cur and len(cur) + len(unit) > cap:
                batches.append({"kind": kind, "items": cur})
                cur = []
            cur.extend(unit)  # unit больше cap -> батч-переросток целиком (get_context разрулит)
        if cur:
            batches.append({"kind": kind, "items": cur})
    return batches


async def _run_batch(pool: asyncpg.Pool, agent, exa: AsyncExa, message: str) -> int:
    """Один батч = одна curator-сессия. Возвращает session_id."""
    session_id = await _new_session(pool)
    session = PostgresSession(f"curator_{session_id}", pool)
    ctx = CuratorRunContext(pool=pool, exa=exa, session_id=session_id)
    snapshot = format_snapshot(await load_snapshot(pool))
    await pool.execute(
        "INSERT INTO curator_messages (session_id, role, content) VALUES ($1, 'user', $2)",
        session_id, message)
    streamed = Runner.run_streamed(agent, input=f"{snapshot}\n\n{message}",
                                   session=session, context=ctx,
                                   max_turns=MAX_TURNS_PER_BATCH)
    await _persist_events(streamed, pool, session_id)
    await pool.execute("UPDATE curator_sessions SET ended_at=now() WHERE id=$1", session_id)
    return session_id


async def _amain(dry_run: bool, max_batches: int | None, types: set[str] | None,
                 dedup: bool) -> None:
    pool = await create_pool(min_size=2, max_size=10)
    try:
        cap = settings.curator_get_context_max_parts
        if dedup:
            rows = await pool.fetch(
                "SELECT id, smart_id, candidate_smart_id, verdict, reason "
                "FROM dedup_candidates WHERE status = 'open' ORDER BY id")
            units = [dict(r) for r in rows]
            batches = [{"kind": "dedup", "items": units[i:i + cap]}
                       for i in range(0, len(units), cap)]
            log(f"[curate] open dedup candidates: {len(units)}; batches: {len(batches)}")
        else:
            items = await _load_hard_list(pool)
            items = [it for it in items if it["kind"] not in _EXCLUDED_TYPES]
            if types:
                items = [it for it in items if it["kind"] in types]
            conf = await _confirmed_sets(pool, [it["run_id"] for it in items])
            batches = _make_batches(items, conf, cap)
            log(f"[curate] hard list items: {len(items)}; batches: {len(batches)}")
        if max_batches is not None:
            batches = batches[:max_batches]

        if dry_run:
            for i, b in enumerate(batches):
                head = [f"{it.get('article', it.get('smart_id'))} ({it.get('reason', '')[:60]})"
                        for it in b["items"]]
                log(f"  batch {i} [{b['kind']}] x{len(b['items'])}: {'; '.join(head[:8])}"
                    + (" …" if len(head) > 8 else ""))
            print(json.dumps({"batches": [{"kind": b["kind"], "size": len(b["items"])}
                                          for b in batches]}, ensure_ascii=False))
            return

        brands, classes = await _load_allowed(pool)
        agent = make_curator_agent(build_curator_system_prompt(brands, classes))
        exa = AsyncExa(api_key=settings.exa_api_key)
        done_sessions = []
        for i, b in enumerate(batches):
            if b["kind"] == "dedup":
                lines = [f"- dedup_candidates.id={it['id']}: {it['smart_id']} vs "
                         f"{it['candidate_smart_id']} [{it['verdict']}] {it['reason']}"
                         for it in b["items"]]
                message = _DEDUP_INSTRUCTIONS.format(items="\n".join(lines))
            else:
                lines = [f"- {it['article']} (run {it['run_id']}): {it['reason']}"
                         for it in b["items"]]
                message = _BATCH_INSTRUCTIONS.format(kind=b["kind"], items="\n".join(lines))
            log(f"[curate] batch {i + 1}/{len(batches)} [{b['kind']}] x{len(b['items'])}")
            sid = await _run_batch(pool, agent, exa, message)
            done_sessions.append(sid)
            log(f"[curate] batch {i + 1} done, session={sid}")
        print(json.dumps({"batches_run": len(done_sessions), "sessions": done_sessions},
                         ensure_ascii=False))
    finally:
        await pool.close()


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    dedup = "--dedup" in args
    max_batches = None
    types: set[str] | None = None
    if "--max-batches" in args:
        max_batches = int(args[args.index("--max-batches") + 1])
    if "--types" in args:
        types = set(args[args.index("--types") + 1].split(","))
    asyncio.run(_amain(dry_run, max_batches, types, dedup))


if __name__ == "__main__":
    main()
