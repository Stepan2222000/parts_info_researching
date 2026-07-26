"""execute_run — полный research-pipeline одного run'а по профилю этапов:

    main -> family_expansion -> low_confidence -> kit_contents ->
    part_of_kits -> price_fallback -> difference -> phase2

Ядро (main + kit_contents + валидации) не отключается; опциональные этапы
включает task_runs.profile (см. research/profiles.py). Kit-этап для наборов
ищет в обе стороны (состав с под-наборами is_kit + родительские наборы);
part_of_kits — «вверх»-поиск для НЕ-наборов, по умолчанию выключен. Каждый этап ведёт
бухгалтерию прогрессивной выдачи: строка в run_turns на старте (running),
снапшот StructuredResult при успехе (ok) либо текст ошибки (failed), плюс
исход в task_runs.stage_outcomes — ошибки этапов не скрываем, best-effort
провал виден потребителю, а не только в логах.

Детерминированный парсинг финального JSON в draft-таблицы — в конце.
Ошибки не скрываем: каждое исключение -> понятный текст в task_runs.error +
соответствующий failure-статус. Никаких маскирующих фолбеков."""

from __future__ import annotations

import json
import traceback
from decimal import Decimal
from typing import Awaitable, Callable

import asyncpg
from exa_py import AsyncExa
from pydantic import ValidationError

from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError

from ..article_format import NOT_CANONICAL, OK, load_ruleset
from ..auto_publish import auto_publish_run
from ..config import settings
from ..db.pool import strip_nul
from ..db.session import PostgresSession
from .agent_factory import make_research_agent
from .context import EBAY_PLUGIN_NAME, SMART_PLUGIN_NAME, load_context
from .errors import NoExactDataError, StructuredOutputInvalid
from .exa_client import cached_exa_call, pick_fetch, pick_search, pick_search_text
from .profiles import ALL_STAGES, resolve_profile, stage_enabled
from .prompts import (
    build_difference_query,
    build_difference_user_message,
    build_family_query,
    build_family_user_message,
    build_kit_contents_query,
    build_kit_contents_user_message,
    build_low_confidence_query,
    build_low_confidence_user_message,
    build_main_fallback_queries,
    build_main_query,
    build_main_user_message,
    build_part_of_query,
    build_part_of_user_message,
    build_phase2_user_message,
    build_price_query,
    build_price_user_message,
    build_repair_user_message,
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


# ── бухгалтерия прогрессивной выдачи (run_turns + stage_outcomes) ───────────────
async def _load_profile(pool: asyncpg.Pool, run_id: int) -> dict:
    """Профиль рана из task_runs.profile. NULL (ран поставлен до деплоя профилей)
    или профиль без ключа repair/auto_publish (ран поставлен до этих флагов) ->
    резолвим (флаги берут env-дефолт) и записываем обратно, чтобы ран был
    самоописанным. Мусор в колонке НЕ глотаем — ValueError уронит ран с текстом
    (failed_crashed)."""
    raw = await pool.fetchval("SELECT profile FROM task_runs WHERE id = $1", run_id)
    profile = resolve_profile(raw)
    if raw is None or (isinstance(raw, dict) and ("repair" not in raw or "auto_publish" not in raw)):
        await pool.execute("UPDATE task_runs SET profile = $1 WHERE id = $2", profile, run_id)
    return profile


async def _init_stage_outcomes(pool: asyncpg.Pool, run_id: int, profile: dict) -> None:
    outcomes = {
        s: ("pending" if stage_enabled(profile, s) else "skipped_by_profile")
        for s in ALL_STAGES
    }
    await pool.execute(
        "UPDATE task_runs SET stage_outcomes = $1 WHERE id = $2", outcomes, run_id
    )


async def _set_outcome(pool: asyncpg.Pool, run_id: int, stage: str, outcome: str) -> None:
    await pool.execute(
        "UPDATE task_runs SET stage_outcomes = coalesce(stage_outcomes, '{}'::jsonb) || $1 "
        "WHERE id = $2",
        {stage: outcome},
        run_id,
    )


async def _turn_start(pool: asyncpg.Pool, run_id: int, turn_idx: int, stage: str) -> None:
    await pool.execute(
        "INSERT INTO run_turns (run_id, turn_idx, stage, status) VALUES ($1, $2, $3, 'running')",
        run_id, turn_idx, stage,
    )


async def _turn_ok(pool: asyncpg.Pool, run_id: int, turn_idx: int, result: StructuredResult) -> None:
    await pool.execute(
        "UPDATE run_turns SET status = 'ok', finished_at = now(), result_json = $1 "
        "WHERE run_id = $2 AND turn_idx = $3",
        result.model_dump(mode="json"), run_id, turn_idx,
    )


async def _turn_failed(pool: asyncpg.Pool, run_id: int, turn_idx: int, error: str) -> None:
    # result_json НЕ трогаем: если этап упал на валидации после снапшота,
    # снапшот остаётся видимым рядом с текстом ошибки.
    await pool.execute(
        "UPDATE run_turns SET status = 'failed', finished_at = now(), error = $1 "
        "WHERE run_id = $2 AND turn_idx = $3",
        error, run_id, turn_idx,
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
    # kit-без-состава больше НЕ needs_review — это критический failed_validation
    # (см. kit-гейт в execute_run).
    if not r.vehicle_classes:
        return "vehicle_classes_unknown"
    return None


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
                article_rows.append((draft_part_id, a.article, "confirmed", a.source_url, a.evidence, None, None))
            for a in r.numbers.article_low_confidence:
                article_rows.append(
                    (draft_part_id, a.article, "low_confidence", a.source_url, a.evidence, a.why_low_confidence, None)
                )
            for a in r.numbers.irrelevant:
                article_rows.append(
                    (draft_part_id, a.article, "irrelevant", a.source_url, a.evidence, None, a.why_irrelevant)
                )
            if article_rows:
                await conn.executemany(
                    "INSERT INTO draft_part_articles "
                    "(draft_part_id, article, confidence, source_url, evidence, why_low_confidence, why_irrelevant) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7)",
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
                     c.name_en, c.description_en, c.is_kit)
                )
            if comp_rows:
                await conn.executemany(
                    "INSERT INTO draft_kit_components "
                    "(draft_part_id, component_key, article, name, quantity, description, source_url, evidence, "
                    " name_en, description_en, is_kit) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
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

            # difference-turn: нюансы (опц. привязка к номерам) и цепочка замен.
            nuance_rows = [
                (draft_part_id, n.text, list(n.articles), n.source_url, n.evidence) for n in r.nuances
            ]
            if nuance_rows:
                await conn.executemany(
                    "INSERT INTO draft_nuances (draft_part_id, text, articles, source_url, evidence) "
                    "VALUES ($1,$2,$3,$4,$5)",
                    nuance_rows,
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
    """Гонит run целиком по профилю этапов. Возвращает финальный статус. Текст любой
    ошибки записан в task_runs.error и пробрасывается наверх (failed_crashed) либо
    отражён в статусе (failed_no_data/failed_validation)."""
    # Гейт «smart уже утверждён»: закрываем до запуска research, без траты модели.
    gate = await _smart_already_approved(pool, article)
    if gate is not None:
        await _finish(pool, run_id, "skipped_smart_approved", gate)
        log(f"[skip] run {run_id} -> skipped_smart_approved ({gate})")
        return "skipped_smart_approved"

    profile = await _load_profile(pool, run_id)
    await _init_stage_outcomes(pool, run_id, profile)
    await _set_running(pool, run_id)
    log(f"[run {run_id}] profile={profile['preset']} stages={profile['stages']}")
    try:
        context = await load_context(pool, article)
        if context.smart_payload is not None:
            await pool.execute(
                "INSERT INTO plugin_payloads (run_id, plugin_name, payload) VALUES ($1,$2,$3)",
                run_id, SMART_PLUGIN_NAME, context.smart_payload,
            )
        # Аудит: что за eBay-подсказку увидела модель (для разбора «почему сказал X»).
        if context.ebay_listings:
            await pool.execute(
                "INSERT INTO plugin_payloads (run_id, plugin_name, payload) VALUES ($1,$2,$3)",
                run_id, EBAY_PLUGIN_NAME,
                {"smart_id": context.smart_payload["id"], "listings": context.ebay_listings},
            )

        session = PostgresSession(f"research_run_{run_id}", pool)
        system_prompt = build_system_prompt(context)
        exa = AsyncExa(api_key=settings.exa_api_key)
        agent = make_research_agent(system_prompt)
        preamble = build_user_preamble(context)

        current: StructuredResult | None = None
        turn = 0
        # Пометки к успешному исходу этапа (напр. "fallback: short" от лестницы
        # main-запросов) — попадают в stage_outcomes как "ok (<note>)".
        stage_notes: dict[str, str] = {}

        def validate(r: StructuredResult) -> None:
            post_validate(
                r,
                expected_part_number=article,
                allowed_brands=context.allowed_brands,
                allowed_vehicle_classes=context.allowed_vehicle_classes,
            )

        repair_enabled = bool(profile["repair"])

        async def repair_turn(turn_idx: int, error_msg: str) -> StructuredResult:
            """Repair: продолжение ТОЙ ЖЕ session (невалидный ответ агента уже в истории)
            коротким сообщением с полным текстом ошибки — без повторных Exa-поисков.
            Базовый agent без тулов: исправление JSON — текстовая задача."""
            return await run_streamed_and_persist(
                agent, build_repair_user_message(error_msg), session, pool, run_id,
                turn_idx, preamble=preamble,
            )

        async def run_stage(
            stage: str,
            fn: Callable[[int], Awaitable[StructuredResult]],
            *,
            best_effort: bool,
        ) -> bool:
            """Один этап с бухгалтерией: run_turns (running -> ok+snapshot / failed+error)
            и stage_outcomes. persist-then-validate: снапшот и result_json пишутся ДО
            post_validate, провал валидации помечает этап failed (снапшот остаётся видим).
            best_effort=True — провал не валит ран, но исход виден как 'failed: ...'.

            repair (profile.repair=true): если модель ОТВЕТИЛА, но ответ не прошёл
            (StructuredOutputInvalid — битый/несхемный JSON; ValidationError/ValueError
            из post_validate) — 1 repair-попытка: текст ошибки возвращается агенту тем же
            session'ом, исправленный результат идёт через тот же validate. Ошибки ДО
            ответа модели (Exa, NoExactDataError, транспорт) не ремонтируются."""
            nonlocal current, turn
            turn += 1
            await _turn_start(pool, run_id, turn, stage)
            await _set_outcome(pool, run_id, stage, "running")
            log(f"[{stage}] turn {turn}")
            model_answered = False
            try:
                result = await fn(turn)
                model_answered = True
                await _turn_ok(pool, run_id, turn, result)
                await _write_result(pool, run_id, result)
                validate(result)
            except Exception as e:  # noqa: BLE001 — исход этапа фиксируем всегда
                msg = f"{type(e).__name__}: {e}"
                await _turn_failed(pool, run_id, turn, msg)
                repairable = isinstance(e, StructuredOutputInvalid) or (
                    model_answered and isinstance(e, (ValidationError, ValueError))
                )
                if not (repair_enabled and repairable):
                    await _set_outcome(pool, run_id, stage, f"failed: {msg}")
                    if not best_effort:
                        raise
                    log(f"[{stage}] failed (non-fatal, keeping prior result): {msg}")
                    return False

                log(f"[{stage}] validation failed, sending error back to agent: {msg}")
                await _set_outcome(pool, run_id, stage, f"repairing: {msg}")
                turn += 1
                await _turn_start(pool, run_id, turn, stage)
                try:
                    result = await repair_turn(turn, msg)
                    await _turn_ok(pool, run_id, turn, result)
                    await _write_result(pool, run_id, result)
                    validate(result)
                except Exception as e2:  # noqa: BLE001 — исход repair фиксируем всегда
                    msg2 = f"{type(e2).__name__}: {e2}"
                    await _turn_failed(pool, run_id, turn, msg2)
                    await _set_outcome(
                        pool, run_id, stage,
                        f"failed: {msg2} (после repair-попытки; исходная ошибка: {msg})",
                    )
                    if not best_effort:
                        raise
                    log(f"[{stage}] repair failed (non-fatal, keeping prior result): {msg2}")
                    return False
                current = result
                note = stage_notes.get(stage)
                await _set_outcome(
                    pool, run_id, stage,
                    f"ok (repaired; {note})" if note else "ok (repaired)",
                )
                log(f"[{stage}] repaired OK")
                return True
            current = result
            note = stage_notes.get(stage)
            await _set_outcome(pool, run_id, stage, f"ok ({note})" if note else "ok")
            return True

        # ── main (ядро, всегда) ────────────────────────────────────────────────
        async def stage_main(turn_idx: int) -> StructuredResult:
            # Лестница запросов: промах substring_check -> следующая формулировка.
            # Агенту уходит выдача ТОЛЬКО успешной ступени: промахнувшиеся выдачи
            # артикула не содержат — там похожие ЧУЖИЕ номера, их в контекст нельзя.
            ladder = [("main", build_main_query(article)), *build_main_fallback_queries(article)]
            picked: str | None = None
            last_err: NoExactDataError | None = None
            for rung, query in ladder:
                raw = await cached_exa_call(
                    pool, exa, "web_search_exa",
                    {"query": query, "num_results": 20},
                    run_id=run_id, phase="main",
                )
                candidate = json.dumps(pick_search(raw), ensure_ascii=False)
                try:
                    substring_check(article, candidate)
                except NoExactDataError as e:
                    last_err = e
                    log(f"[main] substring miss ({rung}), trying next query")
                    continue
                picked = candidate
                if rung != "main":
                    stage_notes["main"] = f"fallback: {rung}"
                break
            if picked is None:
                assert last_err is not None
                raise last_err
            return await run_streamed_and_persist(
                agent, build_main_user_message(article, picked), session, pool, run_id,
                turn_idx, preamble=preamble,
            )

        await run_stage("main", stage_main, best_effort=False)
        assert current is not None  # main строг: сюда доходим только с результатом

        # ── family_expansion (если есть подтверждённые кроссы) ────────────────
        # Засеваем поиск самими ПОДТВЕРЖДЁННЫМИ кроссами (не входным артикулом),
        # чтобы добрать пропущенных «соседей» по семейству. substring_check на
        # входной артикул тут НЕ зовём — на страницах-родственниках его законно
        # может не быть.
        if stage_enabled(profile, "family_expansion"):
            crosses = [a.article for a in current.numbers.article if a.article != article]
            if not crosses:
                await _set_outcome(pool, run_id, "family_expansion", "not_applicable")
            else:
                async def stage_family(turn_idx: int) -> StructuredResult:
                    raw = await cached_exa_call(
                        pool, exa, "web_search_exa",
                        {"query": build_family_query(crosses), "num_results": 10},
                        run_id=run_id, phase="family_expansion",
                    )
                    picked = json.dumps(pick_search(raw), ensure_ascii=False)
                    msg = build_family_user_message(article, picked, current.model_dump_json(indent=2))
                    return await run_streamed_and_persist(
                        agent, msg, session, pool, run_id, turn_idx, preamble=preamble
                    )

                await run_stage("family_expansion", stage_family, best_effort=False)

        # ── low_confidence (если есть сомнительные номера) ─────────────────────
        if stage_enabled(profile, "low_confidence"):
            low_conf = [a.article for a in current.numbers.article_low_confidence]
            if not low_conf:
                await _set_outcome(pool, run_id, "low_confidence", "not_applicable")
            else:
                async def stage_low_conf(turn_idx: int) -> StructuredResult:
                    raw = await cached_exa_call(
                        pool, exa, "web_search_exa",
                        {"query": build_low_confidence_query(article, low_conf), "num_results": 10},
                        run_id=run_id, phase="low_confidence",
                    )
                    picked = json.dumps(pick_search(raw), ensure_ascii=False)
                    msg = build_low_confidence_user_message(picked, current.model_dump_json(indent=2))
                    return await run_streamed_and_persist(
                        agent, msg, session, pool, run_id, turn_idx, preamble=preamble
                    )

                await run_stage("low_confidence", stage_low_conf, best_effort=False)

        # ── kit_contents (ядро; гонится только для наборов) ───────────────────
        # Для набора ищем в ОБЕ стороны одним ходом: состав (вниз, включая
        # под-наборы -> is_kit у компонентов) + родительские наборы (вверх ->
        # part_of_kits). Оба запроса с полным текстом страниц (contents=text):
        # составные таблицы и сервисные chart'ы в highlights почти не попадают.
        if not current.is_kit:
            await _set_outcome(pool, run_id, "kit_contents", "not_applicable")
        else:
            async def stage_kit(turn_idx: int) -> StructuredResult:
                confirmed = [a.article for a in current.numbers.article]
                raw_down = await cached_exa_call(
                    pool, exa, "web_search_exa",
                    {"query": build_kit_contents_query(article, confirmed), "num_results": 10,
                     "contents": "text"},
                    run_id=run_id, phase="kit_contents",
                )
                raw_up = await cached_exa_call(
                    pool, exa, "web_search_exa",
                    {"query": build_part_of_query(article, confirmed), "num_results": 10,
                     "contents": "text"},
                    run_id=run_id, phase="kit_contents",
                )
                picked_down = json.dumps(pick_search_text(raw_down), ensure_ascii=False)
                picked_up = json.dumps(pick_search_text(raw_up), ensure_ascii=False)
                substring_check(article, picked_down + picked_up)
                msg = build_kit_contents_user_message(
                    picked_down, picked_up, current.model_dump_json(indent=2)
                )
                return await run_streamed_and_persist(
                    agent, msg, session, pool, run_id, turn_idx, preamble=preamble
                )

            await run_stage("kit_contents", stage_kit, best_effort=False)

        # Kit-гейт: набор без состава — критический фейл рана (НЕ needs_review).
        if current.is_kit and not current.kit_contents:
            raise ValueError(
                "kit without contents: is_kit=true, but kit_contents is empty after kit_contents stage"
            )

        # ── part_of_kits (опционален; «вверх»-поиск для НЕ-наборов) ────────────
        # В какие наборы входит одиночная деталь. Наборам не нужен — их «вверх»
        # уже искал ядровой kit-этап. substring_check не зовём: релевантная
        # страница набора может писать номер детали в составной таблице любым
        # представлением либо вовсе перечислять только наборы. Best-effort:
        # пустой part_of_kits — нормальный итог, провал этапа не валит ран.
        if stage_enabled(profile, "part_of_kits"):
            if current.is_kit:
                await _set_outcome(pool, run_id, "part_of_kits", "not_applicable")
            else:
                async def stage_part_of(turn_idx: int) -> StructuredResult:
                    confirmed = [a.article for a in current.numbers.article]
                    raw = await cached_exa_call(
                        pool, exa, "web_search_exa",
                        {"query": build_part_of_query(article, confirmed), "num_results": 10,
                         "contents": "text"},
                        run_id=run_id, phase="part_of_kits",
                    )
                    picked = json.dumps(pick_search_text(raw), ensure_ascii=False)
                    msg = build_part_of_user_message(picked, current.model_dump_json(indent=2))
                    return await run_streamed_and_persist(
                        agent, msg, session, pool, run_id, turn_idx, preamble=preamble
                    )

                await run_stage("part_of_kits", stage_part_of, best_effort=True)

        # ── price_fallback (если включён и turn-цены пусты) ────────────────────
        # Отдельный ценовой turn: фокусный US-запрос с полным текстом страниц
        # (user_location=US, contents=text) -> агент заполняет us_prices. Не трогает
        # кэш основного поиска (отдельный ключ). Best-effort: провал не валит ран.
        if stage_enabled(profile, "price_fallback"):
            if current.us_prices:
                await _set_outcome(pool, run_id, "price_fallback", "not_applicable")
            else:
                async def stage_price(turn_idx: int) -> StructuredResult:
                    hint = current.name_en or (current.brand_oem[0] if current.brand_oem else "")
                    raw = await cached_exa_call(
                        pool, exa, "web_search_exa",
                        {"query": build_price_query(article, hint), "num_results": 8,
                         "type": "keyword", "contents": "text", "user_location": "US"},
                        run_id=run_id, phase="price_fallback",
                    )
                    picked = json.dumps(pick_fetch(raw, 4000), ensure_ascii=False)
                    msg = build_price_user_message(article, picked, current.model_dump_json(indent=2))
                    return await run_streamed_and_persist(
                        agent, msg, session, pool, run_id, turn_idx, preamble=preamble
                    )

                if await run_stage("price_fallback", stage_price, best_effort=True):
                    log(f"[price_fallback] us_prices: {len(current.us_prices)}")

        # ── difference (если включён и есть что сравнивать) ────────────────────
        # На ПОДТВЕРЖДЁННЫХ кроссах ищем нюансы между номерами: порядок замен
        # (supersession), нюансы (nuances) и разводку случайно смешанных чужих
        # номеров. Идёт ДО phase2 — нюансы уходят потребителю раньше; phase2 —
        # чистое дозаполнение. Best-effort.
        if stage_enabled(profile, "difference"):
            confirmed_distinct = _distinct_numbers([a.article for a in current.numbers.article])
            if len(confirmed_distinct) < 2:
                await _set_outcome(pool, run_id, "difference", "not_applicable")
                log("[difference] <2 distinct confirmed numbers — нечего сравнивать, пропуск")
            else:
                async def stage_difference(turn_idx: int) -> StructuredResult:
                    raw = await cached_exa_call(
                        pool, exa, "web_search_exa",
                        {"query": build_difference_query(confirmed_distinct), "num_results": 10},
                        run_id=run_id, phase="difference",
                    )
                    picked = json.dumps(pick_search(raw), ensure_ascii=False)
                    msg = build_difference_user_message(article, picked, current.model_dump_json(indent=2))
                    result = await run_streamed_and_persist(
                        agent, msg, session, pool, run_id, turn_idx, preamble=preamble
                    )
                    # Порядок замен — ТОЛЬКО среди подтверждённых номеров: режем рёбра,
                    # у которых любой конец вне numbers.article (сомнительные выкопанные
                    # номера, напр. SKU тюнинг-магазинов, в порядок не идут).
                    confirmed_set = {a.article for a in result.numbers.article}
                    result.supersession = [
                        e for e in result.supersession
                        if e.newer in confirmed_set and e.older in confirmed_set
                    ]
                    return result

                if await run_stage("difference", stage_difference, best_effort=True):
                    log(f"[difference] nuances={len(current.nuances)} supersession={len(current.supersession)}")

        # ── phase2 (опционален, по умолчанию выключен) ─────────────────────────
        # Агентский свободный добор пробелов со своими Exa-тулами. Best-effort.
        if stage_enabled(profile, "phase2"):
            async def stage_phase2(turn_idx: int) -> StructuredResult:
                phase2_agent = make_research_agent(system_prompt, tools=[web_search_exa, web_fetch_exa])
                ctx = Phase2Ctx(pool=pool, exa=exa, run_id=run_id)
                msg = build_phase2_user_message(article, current.model_dump_json(indent=2), PHASE2_EXA_LIMIT)
                result = await run_streamed_and_persist(
                    phase2_agent, msg, session, pool, run_id, turn_idx,
                    context=ctx, max_turns=PHASE2_MAX_TURNS, preamble=preamble,
                )
                log(f"[phase2] exa_calls used: {ctx.exa_calls}/{ctx.limit}")
                return result

            await run_stage("phase2", stage_phase2, best_effort=True)

        # ── финализация: draft + формат-валидация + статус ─────────────────────
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
        # Авто-публикация (profile.auto_publish): сразу в smart без куратора, только
        # однозначный «зелёный коридор» (см. auto_publish.py). ДО выставления статуса:
        # потребитель, увидев терминальный done, видит и auto_publish_outcome.
        # Сбой публикации ран не валит (research уже успешен) — исход в outcome.
        if status == "done" and profile["auto_publish"]:
            outcome = await auto_publish_run(pool, run_id, article)
            log(f"[auto_publish] run {run_id} -> {outcome.get('decision')}: "
                f"{outcome.get('smart_id') or outcome.get('reason') or outcome.get('error')}")
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
