"""execute_run — полный research-pipeline одного run'а (фаза 1: 1-3 turn'а,
фаза 2: агентская). Детерминированный парсинг финального JSON в draft-таблицы.

Ошибки не скрываем: каждое исключение -> понятный текст в task_runs.error +
соответствующий failure-статус. Никаких маскирующих фолбеков."""

from __future__ import annotations

import json
import re
import traceback
from decimal import Decimal

import asyncpg
from exa_py import AsyncExa
from pydantic import ValidationError

from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError

from ..article_format import NOT_CANONICAL, OK, load_ruleset
from ..config import settings
from ..db.pool import strip_nul
from ..db.session import PostgresSession
from .agent_factory import make_research_agent
from .context import SMART_PLUGIN_NAME, load_context
from .errors import NoExactDataError
from .exa_client import cached_exa_call, pick_fetch, pick_search
from .prompts import (
    build_difference_query,
    build_difference_user_message,
    build_family_query,
    build_family_user_message,
    build_kit_contents_query,
    build_kit_contents_user_message,
    build_low_confidence_query,
    build_low_confidence_user_message,
    build_main_query,
    build_main_user_message,
    build_phase2_user_message,
    build_price_query,
    build_price_user_message,
    build_system_prompt,
    build_user_preamble,
)
from .schema import StructuredResult
from .streaming import log, run_streamed_and_persist
from .tools import PHASE2_EXA_LIMIT, Phase2Ctx, web_fetch_exa, web_search_exa
from .validation import post_validate, substring_check

PHASE2_MAX_TURNS = 12


# ── мелкие helpers по task_runs ────────────────────────────────────────────────
async def _set_running(pool: asyncpg.Pool, run_id: int) -> None:
    await pool.execute(
        "UPDATE task_runs SET status = 'running', started_at = now() WHERE id = $1", run_id
    )


async def _write_result(pool: asyncpg.Pool, run_id: int, result: StructuredResult) -> None:
    await pool.execute(
        "UPDATE task_runs SET result_json = $1 WHERE id = $2",
        result.model_dump(mode="json"),
        run_id,
    )


async def _finish(pool: asyncpg.Pool, run_id: int, status: str, error: str | None = None) -> None:
    await pool.execute(
        "UPDATE task_runs SET status = $1, error = $2, finished_at = now() WHERE id = $3",
        status,
        error,
        run_id,
    )


async def _smart_already_approved(pool: asyncpg.Pool, article: str) -> str | None:
    """Гейт: если артикул уже есть в smart.parts с is_unverified=false (состав сверён
    человеком) или is_draft=false (запись финализирована) — research не нужен, задачу
    закрываем. Возвращает причину (с matched smart_id) либо None. Регистронезависимо."""
    row = await pool.fetchrow(
        "SELECT id, is_draft, is_unverified FROM smart.parts "
        "WHERE upper($1) = ANY(ARRAY(SELECT upper(x) FROM unnest(articles) x)) "
        "AND (is_unverified = false OR is_draft = false) ORDER BY id LIMIT 1",
        article,
    )
    if row is None:
        return None
    return f"smart_already_approved: {row['id']} (is_draft={row['is_draft']}, is_unverified={row['is_unverified']})"


def _needs_review_reason(r: StructuredResult) -> str | None:
    reasons: list[str] = []
    if not r.vehicle_classes:
        reasons.append("vehicle_classes_unknown")
    if r.is_kit and not r.kit_contents:
        reasons.append("kit_without_contents")
    return "; ".join(reasons) if reasons else None


# ── фаза 1 ──────────────────────────────────────────────────────────────────────
async def _phase1(
    pool: asyncpg.Pool,
    run_id: int,
    article: str,
    context,
    session: PostgresSession,
    system_prompt: str,
    exa: AsyncExa,
) -> tuple[StructuredResult, int]:
    agent = make_research_agent(system_prompt)
    preamble = build_user_preamble(context)

    def validate(r: StructuredResult) -> None:
        post_validate(
            r,
            expected_part_number=article,
            allowed_brands=context.allowed_brands,
            allowed_vehicle_classes=context.allowed_vehicle_classes,
        )

    # Turn 1 — основной Exa.
    log("[phase1] turn 1 — main Exa")
    raw = await cached_exa_call(
        pool, exa, "web_search_exa",
        {"query": build_main_query(article), "num_results": 10},
        run_id=run_id, phase="main",
    )
    picked = json.dumps(pick_search(raw), ensure_ascii=False)
    substring_check(article, picked)
    current = await run_streamed_and_persist(
        agent, build_main_user_message(article, picked), session, pool, run_id, 1, preamble=preamble
    )
    await _write_result(pool, run_id, current)
    validate(current)
    turn = 1

    # Turn 2 — family-expansion (если у turn 1 есть подтверждённые кроссы):
    # засеваем поиск самими ПОДТВЕРЖДЁННЫМИ кроссами (не входным артикулом), чтобы
    # добрать пропущенных «соседей» по семейству преемственности. substring_check на
    # входной артикул тут НЕ зовём — на страницах-родственниках его законно может не
    # быть. Highlights only: полный текст при нужде дотянет агент в фазе 2.
    crosses = [a.article for a in current.numbers.article if a.article != article]
    if crosses:
        turn += 1
        log(f"[phase1] turn {turn} — family_expansion: {crosses}")
        raw_fam = await cached_exa_call(
            pool, exa, "web_search_exa",
            {"query": build_family_query(crosses), "num_results": 10},
            run_id=run_id, phase="family_expansion",
        )
        picked_fam = json.dumps(pick_search(raw_fam), ensure_ascii=False)
        msg_fam = build_family_user_message(article, picked_fam, current.model_dump_json(indent=2))
        current = await run_streamed_and_persist(agent, msg_fam, session, pool, run_id, turn, preamble=preamble)
        await _write_result(pool, run_id, current)
        validate(current)

    # low_confidence (если есть): пере-классификация уже известных сомнительных
    # номеров — список берём из ОБНОВЛЁННОГО после family результата.
    low_conf = [a.article for a in current.numbers.article_low_confidence]
    if low_conf:
        turn += 1
        log(f"[phase1] turn {turn} — low_confidence: {low_conf}")
        raw2 = await cached_exa_call(
            pool, exa, "web_search_exa",
            {"query": build_low_confidence_query(article, low_conf), "num_results": 10},
            run_id=run_id, phase="low_confidence",
        )
        picked2 = json.dumps(pick_search(raw2), ensure_ascii=False)
        msg2 = build_low_confidence_user_message(picked2, current.model_dump_json(indent=2))
        current = await run_streamed_and_persist(agent, msg2, session, pool, run_id, turn, preamble=preamble)
        await _write_result(pool, run_id, current)
        validate(current)

    # kit_contents (если is_kit).
    if current.is_kit:
        turn += 1
        log(f"[phase1] turn {turn} — kit_contents")
        confirmed = [a.article for a in current.numbers.article]
        raw3 = await cached_exa_call(
            pool, exa, "web_search_exa",
            {"query": build_kit_contents_query(article, confirmed), "num_results": 10},
            run_id=run_id, phase="kit_contents",
        )
        picked3 = json.dumps(pick_search(raw3), ensure_ascii=False)
        substring_check(article, picked3)
        msg3 = build_kit_contents_user_message(picked3, current.model_dump_json(indent=2))
        current = await run_streamed_and_persist(agent, msg3, session, pool, run_id, turn, preamble=preamble)
        await _write_result(pool, run_id, current)
        validate(current)

    return current, turn


# ── ценовой фолбэк (если в turn-1 валидных us_prices не нашлось) ─────────────────
async def _price_fallback(
    pool: asyncpg.Pool,
    run_id: int,
    article: str,
    context,
    session: PostgresSession,
    system_prompt: str,
    exa: AsyncExa,
    current: StructuredResult,
    turn: int,
) -> StructuredResult:
    """Отдельный ценовой turn: фокусный US-запрос с полным текстом страниц
    (user_location=US, contents=text) -> агент заполняет us_prices. Не трогает кэш
    основного поиска (отдельный ключ). Остальные поля JSON агент сохраняет."""
    log("[price] fallback price search (turn-1 us_prices пуст)")
    agent = make_research_agent(system_prompt)
    preamble = build_user_preamble(context)
    hint = current.name_en or (current.brand_oem[0] if current.brand_oem else "")
    raw = await cached_exa_call(
        pool, exa, "web_search_exa",
        {"query": build_price_query(article, hint), "num_results": 8,
         "type": "keyword", "contents": "text", "user_location": "US"},
        run_id=run_id, phase="price_fallback",
    )
    picked = json.dumps(pick_fetch(raw, 4000), ensure_ascii=False)
    msg = build_price_user_message(article, picked, current.model_dump_json(indent=2))
    result = await run_streamed_and_persist(agent, msg, session, pool, run_id, turn, preamble=preamble)
    await _write_result(pool, run_id, result)
    post_validate(
        result,
        expected_part_number=article,
        allowed_brands=context.allowed_brands,
        allowed_vehicle_classes=context.allowed_vehicle_classes,
    )
    log(f"[price] us_prices after fallback: {len(result.us_prices)}")
    return result


# ── фаза 2 ──────────────────────────────────────────────────────────────────────
async def _phase2(
    pool: asyncpg.Pool,
    run_id: int,
    article: str,
    context,
    session: PostgresSession,
    system_prompt: str,
    exa: AsyncExa,
    current: StructuredResult,
    last_turn: int,
) -> StructuredResult:
    log("[phase2] agent-driven free search")
    agent = make_research_agent(system_prompt, tools=[web_search_exa, web_fetch_exa])
    ctx = Phase2Ctx(pool=pool, exa=exa, run_id=run_id)
    preamble = build_user_preamble(context)
    msg = build_phase2_user_message(article, current.model_dump_json(indent=2), PHASE2_EXA_LIMIT)
    turn = last_turn + 1
    result = await run_streamed_and_persist(
        agent, msg, session, pool, run_id, turn, context=ctx, max_turns=PHASE2_MAX_TURNS, preamble=preamble
    )
    await _write_result(pool, run_id, result)
    log(f"[phase2] exa_calls used: {ctx.exa_calls}/{ctx.limit}")
    post_validate(
        result,
        expected_part_number=article,
        allowed_brands=context.allowed_brands,
        allowed_vehicle_classes=context.allowed_vehicle_classes,
    )
    return result


# ── финальный difference-turn ───────────────────────────────────────────────────
def _distinct_numbers(arts: list[str]) -> list[str]:
    """Точный дедуп (case-insensitive по полной строке), сохраняя порядок.
    Дефисы/символы НЕ трогаем: убираем только буквальные повторы, разные номера
    (66015 vs 66015-1) остаются раздельными. Confirmed уже в канон-форме."""
    seen: set[str] = set()
    out: list[str] = []
    for a in arts:
        key = a.strip().upper()
        if key and key not in seen:
            seen.add(key)
            out.append(a)
    return out


async def _difference_turn(
    pool: asyncpg.Pool,
    run_id: int,
    article: str,
    context,
    session: PostgresSession,
    system_prompt: str,
    exa: AsyncExa,
    current: StructuredResult,
    turn: int,
) -> StructuredResult:
    """На ПОДТВЕРЖДЁННЫХ кроссах ищем нюансы между номерами: порядок замен
    (supersession), границы детали (part_caveats), пер-артикульные note. И при OEM-
    пруфе разводим случайно смешанные чужие номера (confirmed -> irrelevant/low).
    Запускается, только если есть что сравнивать (>=2 различных confirmed-номера)."""
    confirmed = _distinct_numbers([a.article for a in current.numbers.article])
    if len(confirmed) < 2:
        log("[difference] <2 distinct confirmed numbers — нечего сравнивать, пропуск")
        return current
    log(f"[difference] turn {turn} — {len(confirmed)} distinct confirmed")
    agent = make_research_agent(system_prompt)
    preamble = build_user_preamble(context)
    raw = await cached_exa_call(
        pool, exa, "web_search_exa",
        {"query": build_difference_query(confirmed), "num_results": 10},
        run_id=run_id, phase="difference",
    )
    picked = json.dumps(pick_search(raw), ensure_ascii=False)
    msg = build_difference_user_message(article, picked, current.model_dump_json(indent=2))
    result = await run_streamed_and_persist(agent, msg, session, pool, run_id, turn, preamble=preamble)
    await _write_result(pool, run_id, result)
    post_validate(
        result,
        expected_part_number=article,
        allowed_brands=context.allowed_brands,
        allowed_vehicle_classes=context.allowed_vehicle_classes,
    )
    log(f"[difference] supersession={len(result.supersession)} "
        f"part_caveats={len(result.part_caveats)} "
        f"notes={sum(1 for a in result.numbers.article if a.note)}")
    return result


# ── парсинг финального JSON -> draft-таблицы ───────────────────────────────────
async def _parse_to_draft(
    pool: asyncpg.Pool, run_id: int, r: StructuredResult, needs_review_reason: str | None,
    derived_product_type: str | None,
) -> None:
    # draft-таблицы пишут поля как plain text (name, evidence, ...), а не jsonb —
    # codec strip_nul их не покрывает; чистим  в самом результате модели.
    r = StructuredResult.model_validate(strip_nul(r.model_dump(mode="json")))
    async with pool.acquire() as conn:
        async with conn.transaction():
            weight_kg = Decimal(str(r.weight.kg)) if r.weight is not None else None
            draft_part_id = await conn.fetchval(
                "INSERT INTO draft_parts ("
                "  run_id, name, brand_oem, vehicle_classes, product_type, description, is_kit, "
                "  weight_kg, weight_source_url, weight_evidence, "
                "  models_text, models_source_urls, models_evidence, needs_review_reason, "
                "  name_en, description_en"
                ") VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16) RETURNING id",
                run_id, r.name, list(r.brand_oem), list(r.vehicle_classes),
                derived_product_type, r.description, r.is_kit,
                weight_kg,
                r.weight.source_url if r.weight else None,
                r.weight.evidence if r.weight else None,
                r.models.text if r.models else None,
                list(r.models.source_urls) if r.models else None,
                r.models.evidence if r.models else None,
                needs_review_reason,
                r.name_en, r.description_en,
            )

            article_rows = []
            for a in r.numbers.article:
                note = (a.note.text, a.note.source_url, a.note.evidence) if a.note else (None, None, None)
                article_rows.append(
                    (draft_part_id, a.article, "confirmed", a.source_url, a.evidence, None, None, *note)
                )
            for a in r.numbers.article_low_confidence:
                article_rows.append(
                    (draft_part_id, a.article, "low_confidence", a.source_url, a.evidence,
                     a.why_low_confidence, None, None, None, None)
                )
            for a in r.numbers.irrelevant:
                article_rows.append(
                    (draft_part_id, a.article, "irrelevant", a.source_url, a.evidence,
                     None, a.why_irrelevant, None, None, None)
                )
            if article_rows:
                await conn.executemany(
                    "INSERT INTO draft_part_articles "
                    "(draft_part_id, article, confidence, source_url, evidence, why_low_confidence, why_irrelevant, "
                    " note_text, note_source_url, note_evidence) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                    article_rows,
                )

            comp_rows = []
            unknown = 0
            for c in r.kit_contents:
                if c.article is not None:
                    key = c.article
                else:
                    unknown += 1
                    key = f"unknown_{unknown}"
                comp_rows.append(
                    (draft_part_id, key, c.article, c.name, c.quantity, c.description, c.source_url, c.evidence,
                     c.name_en, c.description_en)
                )
            if comp_rows:
                await conn.executemany(
                    "INSERT INTO draft_kit_components "
                    "(draft_part_id, component_key, article, name, quantity, description, source_url, evidence, "
                    " name_en, description_en) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                    comp_rows,
                )

            pok_rows = [
                (draft_part_id, p.kit_article, p.kit_name, p.source_url, p.evidence)
                for p in r.part_of_kits
            ]
            if pok_rows:
                await conn.executemany(
                    "INSERT INTO draft_part_of_kits "
                    "(draft_part_id, kit_article, kit_name, source_url, evidence) "
                    "VALUES ($1,$2,$3,$4,$5)",
                    pok_rows,
                )

            # US-цены за оригинал (-> при публикации в parts_prices.market.record_price).
            # CHECK price>0 в draft_prices — отсекаем нулевые/мусорные на всякий случай.
            price_rows = [
                (run_id, p.article, p.site, Decimal(str(p.price)), p.currency or "USD",
                 p.url, p.in_stock, p.evidence)
                for p in r.us_prices if p.price and p.price > 0
            ]
            if price_rows:
                await conn.executemany(
                    "INSERT INTO draft_prices "
                    "(run_id, article, site, price, currency, url, in_stock, evidence) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                    price_rows,
                )

            # difference-turn: границы детали и цепочка замен (с пруфом).
            caveat_rows = [
                (draft_part_id, c.caveat, c.source_url, c.evidence) for c in r.part_caveats
            ]
            if caveat_rows:
                await conn.executemany(
                    "INSERT INTO draft_part_caveats (draft_part_id, caveat, source_url, evidence) "
                    "VALUES ($1,$2,$3,$4)",
                    caveat_rows,
                )

            supersession_rows = [
                (draft_part_id, s.newer, s.older, s.source_url, s.evidence) for s in r.supersession
            ]
            if supersession_rows:
                await conn.executemany(
                    "INSERT INTO draft_supersession (draft_part_id, newer, older, source_url, evidence) "
                    "VALUES ($1,$2,$3,$4,$5)",
                    supersession_rows,
                )


# ── формат-валидация артикулов (persist-then-validate) ──────────────────────────
async def _validate_article_formats(pool: asyncpg.Pool, run_id: int) -> list[str]:
    """Валидирует артикулы УЖЕ записанного draft по канон-правилам brand_mapping и пишет
    проблемы в article_format_problems (FK на draft_part_articles — без дублей). Сами
    ничего не правим. Возвращает список блокирующих confirmed-артикулов (для статуса run'а):
    NOT_CANONICAL блокирует всегда, NO_RULE — только в hard-режиме. Бренд не определён
    (brand_oem пуст) -> формат не гейтим (run и так уйдёт в needs_review)."""
    async with pool.acquire() as conn:
        dp = await conn.fetchrow("SELECT id, brand_oem FROM draft_parts WHERE run_id = $1", run_id)
        if dp is None:
            return []
        brands = set(dp["brand_oem"] or [])
        if not brands:
            return []
        rows = await conn.fetch(
            "SELECT id, article, confidence FROM draft_part_articles "
            "WHERE draft_part_id = $1 AND confidence IN ('confirmed', 'low_confidence')",
            dp["id"],
        )
        ruleset = await load_ruleset(pool)
        hard = settings.format_validation_mode == "hard"
        blocking: list[str] = []
        for r in rows:
            v = ruleset.validate(r["article"], brands)
            if v.status == OK:
                continue
            await conn.execute(
                "INSERT INTO article_format_problems "
                "(draft_article_id, reason, expected_canonical, rule_name, source) "
                "VALUES ($1, $2, $3, $4, 'research') "
                "ON CONFLICT (draft_article_id, source) DO UPDATE SET "
                "  reason = EXCLUDED.reason, expected_canonical = EXCLUDED.expected_canonical, "
                "  rule_name = EXCLUDED.rule_name, created_at = now()",
                r["id"], v.status, v.expected, v.rule_name,
            )
            # блокируем только confirmed (это уходит в smart)
            if r["confidence"] != "confirmed":
                continue
            if v.status == NOT_CANONICAL:
                blocking.append(f"{r['article']} -> {v.expected}")
            elif hard:  # NO_RULE в hard-режиме
                blocking.append(f"{r['article']} (no rule)")
        return blocking


# ── главная функция ──────────────────────────────────────────────────────────────
async def execute_run(pool: asyncpg.Pool, run_id: int, article: str) -> str:
    """Гонит run целиком. Возвращает финальный статус. Текст любой ошибки
    записан в task_runs.error и пробрасывается наверх (failed_crashed) либо
    отражён в статусе (failed_no_data/failed_validation)."""
    # Гейт «smart уже утверждён»: закрываем до запуска research, без траты модели.
    gate = await _smart_already_approved(pool, article)
    if gate is not None:
        await _finish(pool, run_id, "skipped_smart_approved", gate)
        log(f"[skip] run {run_id} -> skipped_smart_approved ({gate})")
        return "skipped_smart_approved"
    await _set_running(pool, run_id)
    try:
        context = await load_context(pool, article)
        if context.smart_payload is not None:
            await pool.execute(
                "INSERT INTO plugin_payloads (run_id, plugin_name, payload) VALUES ($1,$2,$3)",
                run_id, SMART_PLUGIN_NAME, context.smart_payload,
            )

        session = PostgresSession(f"research_run_{run_id}", pool)
        system_prompt = build_system_prompt(context)
        exa = AsyncExa(api_key=settings.exa_api_key)

        current, last_turn = await _phase1(
            pool, run_id, article, context, session, system_prompt, exa
        )
        # Фаза 1 — ядро (результат уже валиден и записан). Ценовой фолбэк и фаза 2 —
        # best-effort дозаполнение: их падение (квота/лимит провайдера, обрыв тулов и т.п.)
        # НЕ должно терять готовый результат фазы 1 — ловим и идём дальше с current.
        if not current.us_prices:
            last_turn += 1
            try:
                current = await _price_fallback(
                    pool, run_id, article, context, session, system_prompt, exa, current, last_turn
                )
            except Exception as e:  # noqa: BLE001 — best-effort, не валим run
                log(f"[price] fallback failed (non-fatal): {type(e).__name__}: {e}")
        try:
            current = await _phase2(
                pool, run_id, article, context, session, system_prompt, exa, current, last_turn
            )
        except Exception as e:  # noqa: BLE001 — best-effort, оставляем результат фазы 1
            log(f"[phase2] failed (non-fatal), keeping pre-phase2 result: {type(e).__name__}: {e}")

        # Финальный difference-turn — нюансы между подтверждёнными кроссами
        # (supersession/part_caveats/note + разводка чужих номеров). Best-effort:
        # падение НЕ теряет готовый результат.
        try:
            current = await _difference_turn(
                pool, run_id, article, context, session, system_prompt, exa, current, last_turn + 2
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            log(f"[difference] failed (non-fatal), keeping prior result: {type(e).__name__}: {e}")

        reason = _needs_review_reason(current)
        # persist-then-validate: сперва пишем draft (всегда), затем формат-валидация
        # читает записанные артикулы и фиксирует проблемы (FK на draft_part_articles).
        await _parse_to_draft(
            pool, run_id, current, reason,
            derived_product_type=context.derive_product_type(list(current.vehicle_classes)),
        )
        blocking = await _validate_article_formats(pool, run_id)
        if blocking:
            msg = "article format not canonical: " + "; ".join(blocking)
            await _finish(pool, run_id, "failed_validation", msg)
            log(f"[fail] run {run_id} -> failed_validation (format): {msg}")
            return "failed_validation"
        status = "needs_human_review" if reason else "done"
        await _finish(pool, run_id, status, error=reason)
        log(f"[done] run {run_id} -> {status}" + (f" ({reason})" if reason else ""))
        return status

    except NoExactDataError as e:
        await _finish(pool, run_id, "failed_no_data", f"{type(e).__name__}: {e}")
        log(f"[fail] run {run_id} -> failed_no_data: {e}")
        return "failed_no_data"
    except (ValidationError, ValueError, MaxTurnsExceeded, ModelBehaviorError) as e:
        await _finish(pool, run_id, "failed_validation", f"{type(e).__name__}: {e}")
        log(f"[fail] run {run_id} -> failed_validation: {e}")
        return "failed_validation"
    except Exception as e:  # noqa: BLE001 — крэш: фиксируем полный traceback, не скрываем
        tb = traceback.format_exc()
        await _finish(pool, run_id, "failed_crashed", f"{type(e).__name__}: {e}\n{tb}")
        log(f"[crash] run {run_id} -> failed_crashed:\n{tb}")
        return "failed_crashed"
