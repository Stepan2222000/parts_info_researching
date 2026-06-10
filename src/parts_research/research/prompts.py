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
    # Keyword-поиск Exa: номер доминирует. Нейронный режим тащил похожие номера,
    # из-за чего точное вхождение не находилось и run уходил в failed_no_data.
    return (
        f"{article} OEM part number cross reference interchange supersedes superseded by "
        "replaces replacement fitment application kit contents"
    )


def build_family_query(crosses: list[str]) -> str:
    # Засеваем уже ПОДТВЕРЖДЁННЫМИ кроссами (не входным номером): цель — выйти на
    # страницы, перечисляющие всё семейство преемственности, и добрать пропущенных
    # «соседей». type не задаём (как у main) — нейтральный режим давал больше
    # семейства в highlights, чем keyword.
    joined = " ".join(crosses)
    return (
        f"{joined} OEM part number supersedes superseded by replaces replaced by "
        "previous version next version interchange cross reference variants"
    )


def build_low_confidence_query(article: str, articles: list[str]) -> str:
    joined = " ".join([article, *articles])
    return (
        f"{joined} OEM cross reference interchange supersedes superseded by "
        "replaces replacement same OEM part"
    )


def build_kit_contents_query(article: str, articles: list[str]) -> str:
    joined = " ".join([article, *articles])
    return (
        f"{joined} kit contents includes components part numbers quantity "
        "contents list parts included exploded diagram"
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
- Каждый номер — РОВНО в одном из numbers.article / numbers.article_low_confidence / numbers.irrelevant, без дублей между массивами (особенно нельзя класть один номер и в article, и в irrelevant).
- brand_oem — массив Smart-брендов (UPPER_SNAKE_CASE из списка выше).
- product_type — одно из допустимых значений или null.
- Пустые строки запрещены: если значения нет — ставь null / пустой массив, не "".
"""


def format_smart_hint(payload: dict[str, Any]) -> str:
    """Выжимка из Smart-плагина для system-prompt (подсказка, может быть устаревшей)."""
    lines = [
        "--- СПРАВОЧНАЯ подсказка из Smart-каталога — ЭТО НЕ ДОКАЗАТЕЛЬСТВО ---",
        "ВНИМАНИЕ: данные ниже могут быть НЕВЕРНЫМИ/устаревшими. Ничего отсюда не копируй вслепую.",
        "Каждое поле и каждый номер бери в результат ТОЛЬКО при независимом подтверждении Exa-источником",
        "(source_url + evidence). Не подтверждено источником — в результат не идёт. Подсказка НЕ повышает",
        "уверенность: номер из подсказки без явного подтверждения источником — максимум",
        "article_low_confidence, не article. При противоречии подсказки и источников — верь источникам.",
        f"name: {payload.get('name')}",
        f"articles: {', '.join(payload.get('articles') or []) or '—'}",
        f"brands: {', '.join(payload.get('brands') or []) or '—'}",
        f"vehicle_classes: {', '.join(payload.get('vehicle_classes') or []) or '—'}",
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
    lines.append("--- Конец справочной подсказки (повторяю: подсказка ≠ доказательство, всё проверяй по источникам) ---")
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


def build_family_user_message(article: str, raw_json: str, current_json: str) -> str:
    return (
        f"Это ДОПОЛНИТЕЛЬНЫЙ поиск, засеянный уже ПОДТВЕРЖДЁННЫМИ OEM-кроссами "
        f"артикула {article} (не самим входным номером), чтобы найти ПРОПУЩЕННЫЕ "
        f"родственные номера того же семейства.\n\n"
        f"Свежий Exa-поиск (url/title/highlights):\n{raw_json}\n\n"
        f"Твой текущий JSON:\n{current_json}\n\n"
        "Добавь найденные новые номера: уверенные (есть явный источник в этом "
        "поиске) → numbers.article; спорные → numbers.article_low_confidence. "
        "Если новый источник явно подтверждает номер, ранее лежавший в "
        "numbers.irrelevant, можешь перенести его в article/low_confidence. "
        "НИКОГДА не выкидывай и не перемещай входной артикул и уже подтверждённые "
        "кроссы. Не добавляй номер, который нечем подкрепить из этого поиска. "
        "Остальные поля сохрани без изменений. Один номер — только в одном массиве. "
        "Ответ — только валидный JSON по схеме."
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
