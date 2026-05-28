"""Сборка системного промпта и user-сообщений всех turn'ов.

Системный промпт одинаков для всех turn'ов одного run'а: правила
research_rules.md + допустимые product_type/бренды/алиасы (из БД через FDW) +
подсказка Smart-плагина (если есть) + напоминание о формате. Бренды/типы/алиасы
берутся из ResearchContext, НЕ хардкодятся.

В каждое user-сообщение фаз 1/2 (кроме turn 1) явно встраивается текущий JSON —
страховка от потери полей (PostgresSession ведёт историю сама, но спека требует
дублировать JSON текстом)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .context import ResearchContext

# research_rules.md лежит в корне репозитория; в Docker-образ кладётся рядом.
# Путь можно переопределить через RESEARCH_RULES_PATH.
_DEFAULT_RULES_PATH = Path(__file__).resolve().parents[3] / "research_rules.md"
RULES_PATH = Path(os.environ.get("RESEARCH_RULES_PATH", _DEFAULT_RULES_PATH))


# ── Exa-запросы фазы 1 (детерминированные формулировки) ────────────────────────
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
- product_type — одно из допустимых значений или null.
- Пустые строки запрещены: если значения нет — ставь null / пустой массив, не "".
"""


def format_smart_hint(payload: dict[str, Any]) -> str:
    """Выжимка из Smart-плагина для system-prompt (подсказка, может быть устаревшей)."""
    lines = [
        "--- Подсказка из Smart-каталога (может быть устаревшей, проверь через источники) ---",
        f"name: {payload.get('name')}",
        f"articles: {', '.join(payload.get('articles') or []) or '—'}",
        f"brands: {', '.join(payload.get('brands') or []) or '—'}",
        f"product_type: {payload.get('product_type')}",
        f"model: {payload.get('model')}",
        f"weight_kg: {payload.get('weight_kg')}",
        f"is_draft: {payload.get('is_draft')}",
    ]
    components = payload.get("components") or []
    if components:
        lines.append("компоненты набора:")
        for c in components:
            lines.append(f"  - {c.get('child_id')} {c.get('name')} x{c.get('quantity')}")
    parents = payload.get("part_of_kits") or []
    if parents:
        lines.append("входит в наборы:")
        for p in parents:
            lines.append(f"  - {p.get('parent_id')} {p.get('name')}")
    lines.append("--- Конец подсказки Smart ---")
    return "\n".join(lines)


def build_system_prompt(context: ResearchContext) -> str:
    rules = RULES_PATH.read_text(encoding="utf-8")
    aliases = "\n".join(
        f"  {alias} -> {canon}" for alias, canon in context.brand_aliases.items()
    )
    smart_hint = ""
    if context.smart_payload is not None:
        smart_hint = "\n" + format_smart_hint(context.smart_payload) + "\n"

    return f"""\
Ты исследуешь OEM-запчасть по входному артикулу и собираешь о ней структурированные \
данные строго на основании предоставленных источников. Отвечай на русском.

Допустимые product_type:
  {", ".join(context.allowed_product_types)}

Допустимые Smart-бренды (brand_oem только из этого списка, UPPER_SNAKE_CASE):
  {", ".join(context.allowed_brands)}

Алиасы OEM-брендов → Smart-бренд (нормализуй найденные бренды к правой части):
{aliases}
{smart_hint}
--- Правила работы (research_rules.md) ---
{rules}
--- Конец правил ---

{SCHEMA_REMINDER}"""


# ── user-сообщения turn'ов ─────────────────────────────────────────────────────
def build_main_user_message(article: str, raw_json: str) -> str:
    return (
        f"Входной артикул: {article}\n"
        f"Основной Exa-поиск (url/title/highlights):\n{raw_json}\n\n"
        "Сформируй стартовый JSON по схеме на основании этих источников."
    )


def build_low_confidence_user_message(raw_json: str, current_json: str) -> str:
    return (
        f"Свежий Exa-поиск по предполагаемым OEM-кроссам:\n{raw_json}\n\n"
        f"Твой текущий JSON:\n{current_json}\n\n"
        "Обнови распределение артикулов по numbers.article / "
        "numbers.article_low_confidence / numbers.irrelevant на основании "
        "новых данных. Все остальные поля сохрани без изменений. "
        "Один артикул не должен попадать в два массива одновременно. "
        "Ответ — только валидный JSON по схеме."
    )


def build_kit_contents_user_message(raw_json: str, current_json: str) -> str:
    return (
        f"Свежий Exa-поиск по составу набора:\n{raw_json}\n\n"
        f"Твой текущий JSON:\n{current_json}\n\n"
        "Обнови kit_contents (массив объектов; article: string|null). "
        "Остальные поля сохрани. Никогда не клади собственный артикул задачи "
        "или артикул из numbers.article как компонент набора. "
        "Ответ — только валидный JSON по схеме."
    )


def build_phase2_user_message(article: str, current_json: str, limit: int) -> str:
    return (
        f"Текущий JSON:\n{current_json}\n\n"
        f"У тебя есть тулы web_search_exa и web_fetch_exa, лимит {limit} вызовов. "
        f"Используй ТОЛЬКО источники, где явно встречается артикул задачи {article}. "
        "Если есть пустые или сомнительные поля — попробуй их закрыть через поиск. "
        "Когда удовлетворён результатом — ответь РОВНО валидным JSON по схеме, без вызова тулов."
    )
