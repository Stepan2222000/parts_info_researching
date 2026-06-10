"""research_part.py — отладочный прогон research-pipeline по одному артикулу.

Полная спецификация — research_part.md (раздел «Алгоритм пошагово») и
PARTS_RESEARCH_SPEC.md (разделы «Research-агент», «Слои валидации»,
«Структура итогового JSON»). Здесь — рабочая реализация прототипа.

Что делает:
  • фаза 1 (1–3 turn'а, backend-driven Exa): main → low_confidence → kit_contents;
  • фаза 2 (agent-driven, @function_tool web_search_exa/web_fetch_exa, лимит 10);
  • печатает финальный StructuredResult (JSON) в stdout.

Чего НЕ делает (в отличие от production): нет БД, нет PostgresSession
(история turn'ов — через result.to_input_list()), нет worker-пула, нет
Smart-плагина, нет файлов состояния на диске. Списки брендов/типов/алиасов
захардкожены (в production берутся из БД через FDW).

Запуск:  python research_part.py [ARTICLE]   (дефолт 76868A04)
Exit:    0 — done; 2 — failed_no_data / failed_validation; 1 — failed_crashed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import httpx
from dotenv import load_dotenv
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import AfterValidator, BaseModel, model_validator

from agents import (
    Agent,
    ModelSettings,
    OpenAIChatCompletionsModel,
    RunContextWrapper,
    Runner,
    function_tool,
    retry_policies,
    set_tracing_disabled,
)
from agents.exceptions import MaxTurnsExceeded
from agents.model_settings import ModelRetrySettings

# ── окружение ─────────────────────────────────────────────────────────────────
load_dotenv()
set_tracing_disabled(True)  # эндпоинт не openai.com — телеметрию SDK отключаем

LLM_BASE_URL = os.environ["LLM_BASE_URL"]
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_MODEL_RESEARCH = os.environ["LLM_MODEL_RESEARCH"]
EXA_API_KEY = os.environ["EXA_API_KEY"]

DEFAULT_ARTICLE = "76868A04"
ARTICLE_RE = re.compile(r"^[A-Z0-9\-]+$")
PHASE2_EXA_LIMIT = 10
PHASE2_MAX_TURNS = 12
TURN_NETWORK_RETRIES = 2  # внешний retry поверх SDK-retry: ловит обрывы посреди стрима

RULES_PATH = Path(__file__).parent / "research_rules.md"


# ── контекст промпта (в production — из БД через FDW) ─────────────────────────
# Список Smart-брендов. В production: SELECT name FROM smart.brands.
ALLOWED_BRANDS: list[str] = [
    "ARCTIC_CAT", "AUDI", "BRP", "HONDA", "LAND_ROVER", "MERCEDES_BENZ",
    "MERCRUISER", "OMC", "POLARIS", "SEASTAR", "SUZUKI", "VOLVO", "YAMAHA",
]

# Классы техники. В production: SELECT slug, title_ru FROM smart.vehicle_classes
# ORDER BY position.
VEHICLE_CLASSES: list[tuple[str, str]] = [
    ("boat", "Катера и лодочные моторы"),
    ("jetski", "Гидроциклы"),
    ("quad", "Квадроциклы и мотовездеходы"),
    ("snowmobile", "Снегоходы"),
    ("motorcycle", "Мотоциклы"),
    ("auto", "Автомобили"),
]
ALLOWED_VEHICLE_CLASSES: list[str] = [slug for slug, _ in VEHICLE_CLASSES]

# Алиасы OEM-брендов → Smart-бренд. В production: SELECT alias, canonical
# FROM brand_mapping.brand_aliases.
BRAND_ALIASES: dict[str, str] = {
    "Mercury": "MERCRUISER",
    "MerCruiser": "MERCRUISER",
    "Quicksilver": "MERCRUISER",
    "Mariner": "MERCRUISER",
    "Mercury Marine": "MERCRUISER",
    "Sea-Doo": "BRP",
    "Ski-Doo": "BRP",
    "Can-Am": "BRP",
    "Evinrude": "OMC",
    "Johnson": "OMC",
    "SeaStar": "SEASTAR",
    "Teleflex": "SEASTAR",
}


# ── ошибки прототипа ───────────────────────────────────────────────────────────
class NoExactDataError(Exception):
    """Exa не вернул источников с точным вхождением артикула (→ failed_no_data)."""


# ── Pydantic-контракт (слои 1 и 2 валидации) ──────────────────────────────────
# Слой 1 — strict JSON schema: структуру/типы/required/additionalProperties=false
#          Agents SDK строит автоматически из output_type и шлёт эндпоинту.
# Слой 2 — Pydantic: non-empty строки через AfterValidator (НЕ Field(min_length=1),
#          чтобы minLength не утёк в JSON-схему и не был отклонён эндпоинтом).
def _nonempty(v: str) -> str:
    if not v.strip():
        raise ValueError("string must be non-empty")
    return v


NonEmptyStr = Annotated[str, AfterValidator(_nonempty)]


class WeightBlock(BaseModel):
    kg: float
    source_url: NonEmptyStr
    evidence: NonEmptyStr


class ModelsBlock(BaseModel):
    text: NonEmptyStr
    source_urls: list[NonEmptyStr]
    evidence: NonEmptyStr

    @model_validator(mode="after")
    def _urls_present(self) -> "ModelsBlock":
        if not self.source_urls:
            raise ValueError("models.source_urls must contain at least one url")
        return self


class ArticleItem(BaseModel):
    article: NonEmptyStr
    source_url: NonEmptyStr
    evidence: NonEmptyStr


class LowConfidenceItem(BaseModel):
    article: NonEmptyStr
    source_url: NonEmptyStr
    evidence: NonEmptyStr
    why_low_confidence: NonEmptyStr


class IrrelevantItem(BaseModel):
    article: NonEmptyStr
    source_url: NonEmptyStr
    evidence: NonEmptyStr
    why_irrelevant: NonEmptyStr


class NumbersBlock(BaseModel):
    article: list[ArticleItem]
    article_low_confidence: list[LowConfidenceItem]
    irrelevant: list[IrrelevantItem]


class KitComponent(BaseModel):
    article: NonEmptyStr | None  # null, если артикул компонента не найден
    name: NonEmptyStr
    quantity: int | None
    description: NonEmptyStr | None
    source_url: NonEmptyStr
    evidence: NonEmptyStr


class PartOfKit(BaseModel):
    kit_article: NonEmptyStr | None  # null, если номер набора неизвестен
    kit_name: NonEmptyStr | None
    source_url: NonEmptyStr
    evidence: NonEmptyStr


class StructuredResult(BaseModel):
    task_part_number: NonEmptyStr
    name: NonEmptyStr | None
    brand_oem: list[NonEmptyStr]
    vehicle_classes: list[NonEmptyStr]  # слаги smart.vehicle_classes; [] = не определены
    description: NonEmptyStr | None
    weight: WeightBlock | None
    models: ModelsBlock | None
    is_kit: bool
    kit_contents: list[KitComponent]
    part_of_kits: list[PartOfKit]
    numbers: NumbersBlock


# ── слой 3: доменная пост-валидация (runtime-данные) ──────────────────────────
def post_validate(
    result: StructuredResult,
    *,
    expected_part_number: str,
    allowed_brands: list[str],
    allowed_vehicle_classes: list[str],
) -> None:
    if result.task_part_number != expected_part_number:
        raise ValueError(
            f"task_part_number {result.task_part_number!r} != expected {expected_part_number!r}"
        )

    bad_brands = [b for b in result.brand_oem if b not in allowed_brands]
    if bad_brands:
        raise ValueError(f"brand_oem not in allowed Smart brands: {bad_brands}")

    bad_classes = [c for c in result.vehicle_classes if c not in allowed_vehicle_classes]
    if bad_classes:
        raise ValueError(f"vehicle_classes not in allowed set: {bad_classes}")
    if len(set(result.vehicle_classes)) != len(result.vehicle_classes):
        raise ValueError("vehicle_classes contains duplicates")

    if not result.is_kit and result.kit_contents:
        raise ValueError("is_kit=false but kit_contents is not empty")

    article_numbers = [a.article for a in result.numbers.article]
    if expected_part_number not in article_numbers:
        raise ValueError("task_part_number is absent from numbers.article")

    # Артикул не должен встречаться более чем в одном из массивов numbers.*
    buckets = {
        "article": {a.article for a in result.numbers.article},
        "article_low_confidence": {a.article for a in result.numbers.article_low_confidence},
        "irrelevant": {a.article for a in result.numbers.irrelevant},
    }
    seen: dict[str, str] = {}
    for bucket_name, arts in buckets.items():
        for art in arts:
            if art in seen:
                raise ValueError(
                    f"article {art!r} appears in both {seen[art]} and {bucket_name}"
                )
            seen[art] = bucket_name

    # Компонент набора не может быть самим артикулом задачи или его OEM-номером
    article_set = set(article_numbers)
    for comp in result.kit_contents:
        if comp.article is None:
            continue
        if comp.article == expected_part_number or comp.article in article_set:
            raise ValueError(
                f"kit component {comp.article!r} duplicates the task article / numbers.article"
            )


# ── клиенты ────────────────────────────────────────────────────────────────────
_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
_model = OpenAIChatCompletionsModel(model=LLM_MODEL_RESEARCH, openai_client=_client)
_retry = ModelSettings(retry=ModelRetrySettings(max_retries=2, policy=retry_policies.network_error()))

# AsyncExa создаётся лениво в main() (после load_dotenv).
_exa: Any = None


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# Опциональный дамп артефактов прогона (только для тестов; в проде всё в БД).
# Включается флагом --dump; по умолчанию None — ничего на диск не пишется.
DUMP_DIR: Path | None = None


def dump(name: str, text: str) -> None:
    if DUMP_DIR is None:
        return
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    (DUMP_DIR / name).write_text(text, encoding="utf-8")
    log(f"  [dump] {DUMP_DIR.name}/{name}")


def is_transient(e: Exception) -> bool:
    """Транспортный обрыв соединения с LLM (повторяем), а не бизнес-ошибка.

    Встроенный SDK-retry часто НЕ ловит обрывы посреди стрима (replay-safety),
    поэтому повторяем turn целиком сами. APIStatusError (4xx/5xx с телом ответа,
    напр. отклонение схемы) и валидационные ошибки сюда НЕ попадают.
    """
    if isinstance(e, (httpx.ReadError, httpx.RemoteProtocolError,
                      httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError)):
        return True
    if isinstance(e, (APIConnectionError, APITimeoutError)):
        return True
    # «Голый» APIError при стриминге (HTTP/2 RST_STREAM / INTERNAL_ERROR) —
    # транспортный сбой, не APIStatusError.
    if isinstance(e, APIError) and not isinstance(e, APIStatusError):
        return True
    return False


# ── backend-Exa (фаза 1) ───────────────────────────────────────────────────────
def _pick_search(results: list[Any]) -> list[dict[str, Any]]:
    """Компактный pick из Exa SearchResponse.results: только url/title/highlights."""
    return [
        {"url": r.url, "title": r.title, "highlights": getattr(r, "highlights", None)}
        for r in results
    ]


async def backend_exa_search(query: str, *, dump_name: str | None = None) -> tuple[list[dict[str, Any]], str]:
    sr = await _exa.search(query, num_results=10, contents={"highlights": True})
    picked = _pick_search(sr.results)
    if dump_name:
        dump(dump_name, json.dumps({"query": query, "results": picked}, ensure_ascii=False, indent=2))
    return picked, json.dumps(picked, ensure_ascii=False)


def substring_check(article: str, raw_json: str) -> None:
    """Точное вхождение артикула без OEM-нормализации (как в спеке).

    Дефисы/префиксы передаются как пришли — артикулы, по которым Exa
    нормализует представление иначе, могут уходить в failed_no_data.
    """
    if article.lower() not in raw_json.lower():
        raise NoExactDataError(f"article {article!r} not found in Exa results")


# ── промпты Exa-поиска фазы 1 (перенесены из research_part.ts) ─────────────────
def build_main_query(article: str) -> str:
    return (
        f'Find information only about the exact part number "{article}".\n\n'
        f'Hard requirement: in every useful source the string "{article}" must explicitly '
        "appear in the page text, title, highlights or description. Do not use similar "
        "numbers, partial matches, transposed digits, or numbers without the letter "
        "suffix.\n\n"
        "Needed: exact part name, OEM brand, old/new part number, superseded by, "
        "replaces, replacement, cross reference, interchange, fitment/application, "
        "kit contents, weight, exploded diagram, parts catalog, PDF, stores and listings.\n\n"
        "OEM only. Aftermarket part numbers are not needed, but if an aftermarket page explicitly "
        f'states the OEM replacement number "{article}" or another OEM number, use only the OEM number.'
    )


def build_low_confidence_query(article: str, articles: list[str]) -> str:
    joined = ", ".join(articles)
    return (
        f"Check whether the part numbers {joined} are OEM cross-numbers, superseded by, "
        f"replaces, replacement, cross reference or interchange for the original part number {article}. "
        "Only OEM relations are needed, aftermarket is not needed. It is important to find sources where "
        f"{article} and the checked part numbers explicitly appear together, or where the relation between "
        "them is stated directly. For each checked part number, find evidence that it is the same OEM "
        "product, or evidence that the relation is not confirmed / it is a different product."
    )


def build_kit_contents_query(article: str, articles: list[str]) -> str:
    joined = ", ".join(articles)
    return (
        f'Find the exact contents of the OEM kit by the original part number "{article}" and the '
        f"confirmed OEM numbers of this same kit in order of currency: {joined}. Look for kit contents, "
        "includes, components, component part numbers, quantity, contents list, parts included, exploded "
        "diagram, parts catalog, PDF. Hard requirement: in every useful source at least one of these OEM "
        f"kit numbers must explicitly appear: {joined}. Needed: component part numbers, component names, "
        "quantity of each component, and a source confirming that the component belongs specifically to "
        "this OEM kit. Aftermarket is not needed."
    )


# ── тулы фазы 2 (@function_tool, счётчик в RunContextWrapper.context) ─────────
@dataclass
class Phase2Ctx:
    exa_calls: int = 0
    limit: int = PHASE2_EXA_LIMIT


_LIMIT_MSG = "Лимит Exa-вызовов исчерпан. Финализируй JSON по схеме без новых вызовов тулов."


@function_tool
async def web_search_exa(
    wrapper: RunContextWrapper[Phase2Ctx], query: str, num_results: int = 10
) -> str:
    """Search the web via Exa for sources about the task article.

    Use this when the current JSON has empty or doubtful fields you want to fill.
    Returns a compact JSON array of {url, title, highlights}.

    Args:
        query: Natural-language or keyword search query.
        num_results: How many results to return (default 10).
    """
    ctx = wrapper.context
    if ctx.exa_calls >= ctx.limit:
        return _LIMIT_MSG
    ctx.exa_calls += 1  # один tool_call = один шаг счётчика
    sr = await _exa.search(query, num_results=num_results, contents={"highlights": True})
    picked = _pick_search(sr.results)
    dump(f"exa_p2_{ctx.exa_calls:02d}_search.json",
         json.dumps({"query": query, "results": picked}, ensure_ascii=False, indent=2))
    return json.dumps(picked, ensure_ascii=False)


@function_tool
async def web_fetch_exa(
    wrapper: RunContextWrapper[Phase2Ctx], urls: list[str], max_characters: int = 3000
) -> str:
    """Fetch the full text of specific URLs via Exa.

    Use this to read a promising source in detail. Returns a compact JSON array
    of {url, title, text}, each text truncated to max_characters.

    Args:
        urls: URLs to fetch (batch several in one call).
        max_characters: Max characters of text per page (default 3000).
    """
    ctx = wrapper.context
    if ctx.exa_calls >= ctx.limit:
        return _LIMIT_MSG
    ctx.exa_calls += 1  # один tool_call = один шаг, независимо от числа urls
    cr = await _exa.get_contents(urls, text=True)
    picked = [
        {"url": r.url, "title": r.title, "text": (getattr(r, "text", "") or "")[:max_characters]}
        for r in cr.results
    ]
    dump(f"exa_p2_{ctx.exa_calls:02d}_fetch.json",
         json.dumps({"urls": urls, "results": picked}, ensure_ascii=False, indent=2))
    return json.dumps(picked, ensure_ascii=False)


# ── системный промпт ───────────────────────────────────────────────────────────
SCHEMA_REMINDER = """\
Формат ответа — РОВНО один JSON-объект по схеме, без markdown-обёртки и комментариев.

Жёсткие правила структуры:
- kit_contents — массив объектов (НЕ объект-словарь). Каждый объект:
  {"article": string|null, "name": string, "quantity": int|null,
   "description": string|null, "source_url": string, "evidence": string}.
  Поля "key" нет.
- Если is_kit=false — kit_contents=[] (пустой массив).
- numbers.article, numbers.article_low_confidence, numbers.irrelevant — массивы объектов.
- task_part_number обязан присутствовать в numbers.article.
- brand_oem — массив Smart-брендов (UPPER_SNAKE_CASE из списка выше).
- vehicle_classes — массив слагов классов техники из списка выше (можно несколько,
  в т.ч. разных типов — например, общая деталь гидроциклов и снегоходов:
  ["jetski","snowmobile"]). Не смог определить — пустой массив [].
- Пустые строки запрещены: если значения нет — ставь null / пустой массив, не "".
"""


def build_system_prompt() -> str:
    rules = RULES_PATH.read_text(encoding="utf-8")
    aliases = "\n".join(f"  {alias} -> {canon}" for alias, canon in BRAND_ALIASES.items())
    return f"""\
Ты исследуешь OEM-запчасть по входному артикулу и собираешь о ней структурированные \
данные строго на основании предоставленных источников. Отвечай на русском.

Классы техники (vehicle_classes — слаги только из этого списка):
{chr(10).join(f"  {slug} — {title}" for slug, title in VEHICLE_CLASSES)}

Допустимые Smart-бренды (brand_oem только из этого списка, UPPER_SNAKE_CASE):
  {", ".join(ALLOWED_BRANDS)}

Алиасы OEM-брендов → Smart-бренд (нормализуй найденные бренды к правой части):
{aliases}

--- Правила работы (research_rules.md) ---
{rules}
--- Конец правил ---

{SCHEMA_REMINDER}"""


# ── фаза 1 ────────────────────────────────────────────────────────────────────
def _phase1_agent() -> Agent:
    return Agent(
        name="research-phase1",
        instructions=build_system_prompt(),
        model=_model,
        model_settings=_retry,
        output_type=StructuredResult,
    )


async def phase1(article: str) -> tuple[StructuredResult, list[Any]]:
    agent = _phase1_agent()

    # Turn 1 — основной Exa.
    log("[phase1] turn 1 — main Exa")
    _, raw1 = await backend_exa_search(build_main_query(article), dump_name="exa_p1_main.json")
    substring_check(article, raw1)
    msg1 = (
        f"Входной артикул: {article}\n"
        f"Основной Exa-поиск (url/title/highlights):\n{raw1}\n\n"
        "Сформируй стартовый JSON по схеме на основании этих источников."
    )
    streamed = await _run_and_capture(agent, msg1, label="p1.t1")
    current, history = streamed
    post_validate(current, expected_part_number=article,
                  allowed_brands=ALLOWED_BRANDS, allowed_vehicle_classes=ALLOWED_VEHICLE_CLASSES)

    # Turn 2 — low_confidence (если есть).
    low_conf = [a.article for a in current.numbers.article_low_confidence]
    if low_conf:
        log(f"[phase1] turn 2 — low_confidence: {low_conf}")
        _, raw2 = await backend_exa_search(
            build_low_confidence_query(article, low_conf), dump_name="exa_p1_low_confidence.json"
        )
        msg2 = history + [{
            "role": "user",
            "content": (
                f"Свежий Exa-поиск по предполагаемым OEM-кроссам:\n{raw2}\n\n"
                f"Твой текущий JSON:\n{current.model_dump_json(indent=2)}\n\n"
                "Обнови распределение артикулов по numbers.article / "
                "numbers.article_low_confidence / numbers.irrelevant на основании "
                "новых данных. Все остальные поля сохрани без изменений. "
                "Один артикул не должен попадать в два массива одновременно. "
                "Ответ — только валидный JSON по схеме."
            ),
        }]
        current, history = await _run_and_capture(agent, msg2, label="p1.t2")
        post_validate(current, expected_part_number=article,
                      allowed_brands=ALLOWED_BRANDS, allowed_vehicle_classes=ALLOWED_VEHICLE_CLASSES)

    # Turn 3 — kit_contents (если is_kit).
    if current.is_kit:
        log("[phase1] turn 3 — kit_contents")
        confirmed = [a.article for a in current.numbers.article]
        _, raw3 = await backend_exa_search(
            build_kit_contents_query(article, confirmed), dump_name="exa_p1_kit.json"
        )
        substring_check(article, raw3)
        msg3 = history + [{
            "role": "user",
            "content": (
                f"Свежий Exa-поиск по составу набора:\n{raw3}\n\n"
                f"Твой текущий JSON:\n{current.model_dump_json(indent=2)}\n\n"
                "Обнови kit_contents (массив объектов; article: string|null). "
                "Остальные поля сохрани. Никогда не клади собственный артикул задачи "
                "или артикул из numbers.article как компонент набора. "
                "Ответ — только валидный JSON по схеме."
            ),
        }]
        current, history = await _run_and_capture(agent, msg3, label="p1.t3")
        post_validate(current, expected_part_number=article,
                      allowed_brands=ALLOWED_BRANDS, allowed_vehicle_classes=ALLOWED_VEHICLE_CLASSES)

    return current, history


async def _run_and_capture(
    agent: Agent, input_data: Any, *, label: str,
    context: Any = None, max_turns: int | None = None,
) -> tuple[StructuredResult, list[Any]]:
    """Запускает turn и возвращает (final_output, to_input_list для следующего turn'а)."""
    last_exc: Exception | None = None
    for attempt in range(1, TURN_NETWORK_RETRIES + 2):
        try:
            kwargs: dict[str, Any] = {}
            if context is not None:
                kwargs["context"] = context
            if max_turns is not None:
                kwargs["max_turns"] = max_turns
            streamed = Runner.run_streamed(agent, input=input_data, **kwargs)
            async for ev in streamed.stream_events():
                if ev.type == "raw_response_event":
                    continue
                if ev.type == "agent_updated_stream_event":
                    log(f"  [{label}] agent: {ev.new_agent.name}")
                elif ev.type == "run_item_stream_event":
                    if ev.name == "tool_called":
                        raw = getattr(ev.item, "raw_item", None)
                        tname = getattr(raw, "name", "?")
                        targs = str(getattr(raw, "arguments", ""))[:120]
                        log(f"  [{label}] tool_called: {tname}({targs})")
                    else:
                        log(f"  [{label}] {ev.name}")
            result = streamed.final_output
            if not isinstance(result, StructuredResult):
                raise ValueError(f"final_output is not StructuredResult: {type(result)}")
            return result, streamed.to_input_list()
        except Exception as e:  # noqa: BLE001
            if not is_transient(e):
                raise
            last_exc = e
            log(f"  [{label}] transport error (attempt {attempt}): {type(e).__name__}: {e}")
    assert last_exc is not None
    raise last_exc


# ── фаза 2 ────────────────────────────────────────────────────────────────────
async def phase2(article: str, current: StructuredResult, history: list[Any]) -> StructuredResult:
    log("[phase2] agent-driven free search")
    agent = Agent(
        name="research-phase2",
        instructions=build_system_prompt(),
        model=_model,
        model_settings=_retry,
        output_type=StructuredResult,
        tools=[web_search_exa, web_fetch_exa],
    )
    ctx = Phase2Ctx()
    msg = history + [{
        "role": "user",
        "content": (
            f"Текущий JSON:\n{current.model_dump_json(indent=2)}\n\n"
            f"У тебя есть тулы web_search_exa и web_fetch_exa, лимит {PHASE2_EXA_LIMIT} вызовов. "
            f"Используй ТОЛЬКО источники, где явно встречается артикул задачи {article}. "
            "Если есть пустые или сомнительные поля — попробуй их закрыть через поиск. "
            "Когда удовлетворён результатом — ответь РОВНО валидным JSON по схеме, без вызова тулов."
        ),
    }]
    result, _ = await _run_and_capture(
        agent, msg, label="p2", context=ctx, max_turns=PHASE2_MAX_TURNS
    )
    log(f"[phase2] exa_calls used: {ctx.exa_calls}/{ctx.limit}")
    post_validate(result, expected_part_number=article,
                  allowed_brands=ALLOWED_BRANDS, allowed_vehicle_classes=ALLOWED_VEHICLE_CLASSES)
    return result


# ── main ──────────────────────────────────────────────────────────────────────
def pre_validate_article(raw: str) -> str:
    article = raw.strip().upper()
    if not ARTICLE_RE.match(article):
        raise ValueError(f"article {article!r} fails regex ^[A-Z0-9\\-]+$")
    return article


async def main() -> int:
    global _exa, DUMP_DIR
    from exa_py import AsyncExa

    _exa = AsyncExa(api_key=EXA_API_KEY)

    # CLI: первый позиционный аргумент — артикул; флаг --dump[=DIR] включает дамп
    # артефактов прогона (Exa-ответы + финальный JSON) в DIR/<article>/.
    # По умолчанию DIR = test_runs. Без --dump на диск ничего не пишется (как в проде).
    args = sys.argv[1:]
    dump_base: str | None = None
    positional: list[str] = []
    for arg in args:
        if arg == "--dump":
            dump_base = "test_runs"
        elif arg.startswith("--dump="):
            dump_base = arg.split("=", 1)[1] or "test_runs"
        else:
            positional.append(arg)

    raw_article = positional[0] if positional else DEFAULT_ARTICLE
    try:
        article = pre_validate_article(raw_article)
    except ValueError as e:
        log(f"[error] ValueError: {e}")
        return 2

    if dump_base:
        DUMP_DIR = Path(dump_base) / article

    log(f"=== research_part.py — article {article} ===")
    try:
        current, history = await phase1(article)
        current = await phase2(article, current, history)
    except NoExactDataError as e:
        log(f"[error] NoExactDataError: {e}  (failed_no_data)")
        return 2
    except MaxTurnsExceeded as e:
        log(f"[error] MaxTurnsExceeded: {e}  (failed_validation)")
        return 2
    except ValueError as e:
        log(f"[error] ValueError: {e}  (failed_validation)")
        return 2
    except Exception as e:  # noqa: BLE001 — сетевые/прочие после ретраев
        log(f"[error] {type(e).__name__}: {e}  (failed_crashed)")
        log(traceback.format_exc())
        return 1

    dump("result.json", current.model_dump_json(indent=2))
    log("[final json] ↓ stdout")
    print(current.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
