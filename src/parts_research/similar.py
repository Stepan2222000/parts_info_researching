"""Воронка похожести (анти-дубли): детерминированный шорт-лист + LLM-судья.

Замеры на живой очереди (2026-07-26, 1548 ранов × 1098 записей smart) показали:
ни один порог текстовой похожести сам по себе не работает — медианная похожесть
пар с общим артикулом (заведомо одна деталь) всего 0.41, а топ рейтинга — родовые
имена («Сальник масляный» ↔ «Сальник масляный»), где совпадение ничего не значит.
Поэтому конструкция трёхступенчатая:

  1. Мягкий скор (имя RU/EN, модели, описание, бонусы за бренд/класс; БЕЗ жёстких
     вето — они убивают настоящие дубли) ранжирует каталог, берём топ-K.
     Recall настоящего дубля в топ-80 — 98%, в топ-40 — 96.7%.
  2. LLM-судья (дешёвая модель, strict-схема) смотрит карточки ДО публикации
     новой записи и решает: same / unsure / different. same требует совпадения
     самой детали И применяемости (модели, если указаны у обеих сторон, обязаны
     пересекаться). Родовое имя без спек — unsure; конфликт размеров/шага/
     вращения — different (проверено на стресс-кейсах: винты по шагу и стороне
     вращения различает, сальники не склеивает).
  3. same придерживает публикацию: ран уходит в hard list с пометкой похожести,
     куратор с web-пруфом либо вливает его в существующую запись (save_to_smart
     со smart_id), либо публикует новой. unsure/different не блокируют.
     Автослияний и авто-отказов по имени нет ни при каком пороге.

Оркестровка пре-чека — в auto_publish._similar_precheck (гейты и advisory-lock
живут там); здесь — данные, скоринг и судья.

Извлечение спек: размерные цепочки (30x60x37, 14x19), резьбы (M8x1.25), дюймовые
дроби (1-1/16) из имени и описания — подсветка для судьи, не критерий отбора.
"""

from __future__ import annotations

import difflib
import json
import re

import asyncpg
from openai import AsyncOpenAI

from .config import settings

# ── нормализация текста ───────────────────────────────────────────────────────
_STOP = {"для", "и", "с", "на", "в", "по", "the", "for", "and", "of", "with", "kit",
         "комплект", "ремкомплект", "набор", "assy", "assembly", "оригинал",
         "оригинальный", "genuine", "oem"}
_BRAND_WORDS = {"mercury", "mercruiser", "quicksilver", "mariner", "brp", "sea-doo",
                "seadoo", "ski-doo", "skidoo", "can-am", "canam", "lynx", "rotax",
                "volvo", "penta", "suzuki", "yamaha", "honda", "polaris", "kawasaki",
                "arctic", "cat", "johnson", "evinrude", "omc", "tohatsu", "nissan",
                "marine"}
_ART_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{4,}$")

_DIM_RE = re.compile(r"\d+(?:[.,]\d+)?(?:\s*[x×х]\s*\d+(?:[.,]\d+)?)+", re.I)
_THREAD_RE = re.compile(r"\bm\d+(?:[.,]\d+)?(?:x\d+(?:[.,]\d+)?)?\b", re.I)
_FRACT_RE = re.compile(r"\b\d+(?:-\d+)?/\d+\b")


def _norm_dim(s: str) -> str:
    s = s.lower().replace(",", ".").replace("×", "x").replace("х", "x")
    return re.sub(r"\s+", "", s)


def extract_specs(*texts: str | None) -> list[str]:
    """Размеры/резьбы/дроби из текстов — подсветка сигналов для судьи."""
    specs: set[str] = set()
    for t in texts:
        if not t:
            continue
        for rx in (_DIM_RE, _THREAD_RE, _FRACT_RE):
            specs.update(_norm_dim(m) for m in rx.findall(t))
    return sorted(specs)


def _name_tokens(name: str | None) -> set[str]:
    if not name:
        return set()
    out: set[str] = set()
    for t in re.findall(r"[a-zа-яё0-9\-/]+", name.lower()):
        t = t.strip("-/")
        if not t or t in _STOP or t in _BRAND_WORDS:
            continue
        if _ART_TOKEN_RE.fullmatch(t) and any(c.isdigit() for c in t):
            continue  # токены-артикулы из имён словами не считаем
        out.add(t)
    return out


def _model_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {t for t in re.findall(r"[a-zа-яё0-9.\-]+", text.lower())
            if len(t) >= 2 and t not in _STOP}


def _jac(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def soft_score(draft: dict, cand: dict) -> float:
    """Мягкий скор для ранжирования шорт-листа. Без вето: несовпадение класса или
    конфликт размеров — сигнал судье, не причина выкинуть кандидата (замер: жёсткие
    вето убивали настоящие дубли с расхождением классов между раном и каталогом)."""
    name_sims = []
    for dn in (draft.get("name"), draft.get("name_en")):
        for sn in (cand.get("name"), cand.get("name_en")):
            if dn and sn:
                name_sims.append(
                    0.6 * _jac(_name_tokens(dn), _name_tokens(sn))
                    + 0.4 * difflib.SequenceMatcher(None, dn.lower(), sn.lower()).ratio())
    name_sim = max(name_sims, default=0.0)
    model_sim = _jac(_model_tokens(draft.get("models")), _model_tokens(cand.get("models")))
    descr_sim = _jac(_name_tokens(draft.get("description")), _name_tokens(cand.get("description")))
    brand_ok = bool({b.upper() for b in draft.get("brands") or []}
                    & {b.upper() for b in cand.get("brands") or []})
    class_ok = (not draft.get("classes") or not cand.get("classes")
                or bool(set(draft["classes"]) & set(cand["classes"])))
    return (0.55 * name_sim + 0.25 * model_sim + 0.10 * descr_sim
            + (0.05 if brand_ok else 0.0) + (0.05 if class_ok else 0.0))


# ── данные ────────────────────────────────────────────────────────────────────
async def load_catalog(conn: asyncpg.Connection) -> list[dict]:
    """Каталог smart целиком (id, имена RU/EN, модели, описание, бренды, классы,
    артикулы). Четыре плоских скана вместо коррелированных подзапросов — через FDW
    так на порядок быстрее (нет remote-обращения на каждую строку)."""
    parts = await conn.fetch(
        "SELECT id, name, model, description, articles FROM smart.parts")
    en = {r["part_id"]: r for r in await conn.fetch(
        "SELECT part_id, name, description FROM smart.parts_en")}
    brands: dict[str, list[str]] = {}
    for r in await conn.fetch("SELECT part_id, brand FROM smart.part_brands"):
        brands.setdefault(r["part_id"], []).append(r["brand"])
    classes: dict[str, list[str]] = {}
    for r in await conn.fetch("SELECT part_id, class_slug FROM smart.part_vehicle_classes"):
        classes.setdefault(r["part_id"], []).append(r["class_slug"])
    out = []
    for p in parts:
        e = en.get(p["id"])
        out.append({
            "id": p["id"], "name": p["name"], "name_en": e["name"] if e else None,
            "models": p["model"], "description": p["description"],
            "description_en": e["description"] if e else None,
            "articles": list(p["articles"] or []),
            "brands": brands.get(p["id"], []), "classes": classes.get(p["id"], []),
        })
    return out


async def load_draft_card(conn: asyncpg.Connection, run_id: int) -> dict | None:
    """Карточка рана для воронки (та же форма полей, что у записи каталога)."""
    dp = await conn.fetchrow(
        "SELECT id, name, name_en, brand_oem, vehicle_classes, models_text, "
        "description, description_en FROM draft_parts WHERE run_id = $1", run_id)
    if dp is None:
        return None
    confirmed = [r["article"] for r in await conn.fetch(
        "SELECT article FROM draft_part_articles "
        "WHERE draft_part_id = $1 AND confidence = 'confirmed' ORDER BY id", dp["id"])]
    return {
        "name": dp["name"], "name_en": dp["name_en"],
        "models": dp["models_text"], "description": dp["description"],
        "description_en": dp["description_en"],
        "brands": list(dp["brand_oem"] or []),
        "classes": list(dp["vehicle_classes"] or []),
        "articles": confirmed,
    }


def build_shortlist(draft: dict, catalog: list[dict],
                    k: int | None = None, exclude_ids: set[str] | None = None) -> list[dict]:
    """Топ-K кандидатов каталога по мягкому скору: [{score, **запись каталога}]."""
    k = k or settings.dedup_shortlist_k
    exclude_ids = exclude_ids or set()
    scored = [(soft_score(draft, c), c) for c in catalog if c["id"] not in exclude_ids]
    scored.sort(key=lambda x: -x[0])
    return [{"score": round(s, 3), **c} for s, c in scored[:k]]


# ── LLM-судья ─────────────────────────────────────────────────────────────────
_client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "verdict": {"type": "string",
                                "enum": ["same", "unsure", "different"]},
                    "reason": {"type": "string"},
                },
                "required": ["candidate_id", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

_JUDGE_SYS = """Ты — эксперт по каталогу запчастей для лодочных моторов, гидроциклов, снегоходов, квадроциклов, мотоциклов и авто.
Дана карточка новой детали и записи каталога, похожие на неё по имени. У них НЕТ общих артикулов — иначе они бы сматчились раньше. По каждой записи реши, ЭТО ТА ЖЕ ДЕТАЛЬ (дубль в каталоге) или нет:
- same: уверен, что тот же товар — описывается одна и та же запчасть И применяемость сходится.
  Если модели указаны у обеих сторон, они обязаны пересекаться; указаны и НЕ пересекаются —
  это НЕ same (скорее different: похожая деталь для другой техники). Если у кандидата модели
  не указаны — решай по остальному (имя, размеры, бренд, описание) и ставь same только при
  полной уверенности.
- unsure: невозможно решить по данным (типовая деталь без размеров: сальник, кольцо, прокладка
  без спек; или данных мало, чтобы утверждать «тот же товар»)
- different: другая деталь (другой размер/шаг/сторона вращения/другое применение/другой узел)
ВАЖНО: одинаковое родовое название («сальник», «прокладка», «o-ring») само по себе НЕ значит тот же товар.
Конфликт размеров/шага/вращения = different. Решай только по данным карточек, ничего не выдумывай."""


def _judge_card(x: dict, with_id: bool) -> dict:
    card = {
        "name": x.get("name"), "name_en": x.get("name_en"),
        "brands": x.get("brands") or [],
        "articles": x.get("articles") or [],
        "models": (x.get("models") or "")[:500] or None,
        "description": (x.get("description") or "")[:300] or None,
        "specs_extracted": extract_specs(x.get("name"), x.get("name_en"),
                                         x.get("description")) or None,
    }
    if with_id:
        card = {"id": x["id"], **card}
    return card


def _model_base(name: str) -> str:
    """gpt-5.6-luna(high) -> gpt-5.6-luna: прокси суффикс усилия в ответе не возвращает."""
    return re.sub(r"\(.*\)$", "", name).strip()


async def judge_json(system: str, user_content: str, schema: dict, schema_name: str) -> dict:
    """Один вызов judge-модели со strict JSON-схемой. Сверяем поле model ответа:
    прокси молча глотает неверные имена моделей (проверено на этом эндпоинте)."""
    resp = await _client.chat.completions.create(
        model=settings.llm_model_judge,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user_content}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": schema_name, "strict": True,
                                         "schema": schema}},
        max_tokens=30000,
    )
    want = _model_base(settings.llm_model_judge)
    got = _model_base(resp.model or "")
    if got != want:
        raise RuntimeError(f"judge model mismatch: requested {want!r}, proxy served {got!r}")
    return json.loads(resp.choices[0].message.content)


async def judge_shortlist(draft: dict, shortlist: list[dict]) -> list[dict]:
    """Один вызов дешёвой модели на весь шорт-лист. Возвращает вердикты по каждому
    кандидату (мусорные candidate_id вне шорт-листа отбрасываются)."""
    payload = {
        "new_part": _judge_card(draft, with_id=False),
        "catalog_candidates": [_judge_card(c, with_id=True) for c in shortlist],
    }
    out = await judge_json(_JUDGE_SYS, json.dumps(payload, ensure_ascii=False),
                           _JUDGE_SCHEMA, "verdicts")
    known = {c["id"] for c in shortlist}
    return [v for v in out["verdicts"] if v["candidate_id"] in known]
