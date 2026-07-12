"""Сборка системного промпта и user-сообщений всех turn'ов.

Системный промпт одинаков для всех turn'ов одного run'а: правила
research_rules.md + классы техники/бренды/алиасы (из БД через FDW) +
подсказка Smart-плагина (если есть) + напоминание о формате. Бренды/классы/алиасы
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
from .schema import StructuredResult

_STRUCT_SCHEMA_JSON = json.dumps(StructuredResult.model_json_schema(), ensure_ascii=False)


def build_user_preamble(context: ResearchContext) -> str:
    """Критичные ограничения В USER-СООБЩЕНИИ (а не в system): текущий LLM-эндпоинт
    (cli-proxy → Codex) игнорирует system-инструкции и response_format, но слушает
    user-сообщение. Сюда — JSON Schema, точные имена полей, и enum'ы brand_oem/
    vehicle_classes с алиасами. Без этого модель выдаёт свои поля и display-бренды."""
    aliases = "\n".join(f"  {a} -> {c}" for a, c in context.brand_aliases.items())
    classes = "\n".join(f"  {vc.slug} — {vc.title_ru}" for vc in context.vehicle_classes)
    return (
        "ФОРМАТ ОТВЕТА (ОБЯЗАТЕЛЬНО): верни РОВНО один JSON-объект строго по JSON Schema ниже. "
        "Имена полей — ТОЧНО: task_part_number, name, name_en, brand_oem, vehicle_classes, "
        "description, description_en, weight, models, is_kit, kit_contents, part_of_kits, numbers, "
        "us_prices, nuances, supersession. НЕ придумывай свои поля (никаких input_article/"
        "oem_part_number/pricing/sources/replaces/applications). БЕЗ markdown-обёртки (никаких ```), "
        "без текста до/после — только JSON.\n\n"
        f"brand_oem — ТОЛЬКО из этого списка Smart-брендов (UPPER_SNAKE_CASE): {', '.join(context.allowed_brands)}\n"
        f"Нормализуй найденные бренды по алиасам (левое -> правое), не пиши display-имена:\n{aliases}\n\n"
        f"vehicle_classes — ТОЛЬКО слаги из списка (можно несколько; [] если не определил):\n{classes}\n\n"
        "ЖЁСТКИЕ ПРАВИЛА (иначе результат отклоняется):\n"
        "- numbers.article ОБЯЗАН включать сам входной артикул (task_part_number) с source_url и evidence.\n"
        "- Каждый номер — РОВНО в одном из numbers.article / numbers.article_low_confidence / "
        "numbers.irrelevant, без дублей между ними.\n"
        "- Пустые строки запрещены: нет значения -> null или [] (не \"\").\n"
        "- АНГЛИЙСКИЙ ОБЯЗАТЕЛЕН: при непустом name заполни name_en, при непустом description — "
        "description_en (то же для компонентов kit_contents). EN кладётся в *_en, НЕ в скобки русского.\n"
        "- name / name_en — КОРОТКИЕ: из них собирается title фида \"<арт1> / <арт2> <name>\" с лимитом "
        "50 символов (ран ОТКЛОНЯЕТСЯ при превышении). Два артикула съедают ~22 символа, поэтому на само "
        "имя остаётся ~25 — держись этого и для name, и для name_en. Пиши ТОЛЬКО тип детали + отличительную "
        "спеку (размер/шаг/вариант): \"Форсунка топливная\"/\"Fuel injector\", \"Винт гребной 12.5x19P RH\"/"
        "\"Propeller 12.5x19P RH\". БЕЗ бренда (он в brand_oem), БЕЗ модели/двигателя (она в model), без "
        "\"OEM\" и расшифровок. Идентифицирующую спеку винта (размер/шаг/вращение) НЕ выкидывай — ужимай "
        "формулировку, пока не влезет. Подробности (материал, назначение) — в description/description_en.\n\n"
        "description / description_en: кратко ЧТО это за деталь, её НАЗНАЧЕНИЕ/функция и где применяется "
        "(узел, тип техники). 1–2 предложения, без воды и маркетинга. НЕ пиши в описании кросс-/замен-номера, "
        "'заменён номером X' / 'replaces X', артикулы, SKU и цены — это уже в своих полях (numbers/us_prices).\n\n"
        "ФОРМАТ АРТИКУЛОВ (numbers.article / article_low_confidence / irrelevant): пиши КАЖДЫЙ номер в "
        "КАНОНИЧЕСКОЙ форме каталога. Приводи к канону ТОЛЬКО по правилам ниже (например, у Mercury убирай "
        "ведущий дилерский префикс NN-: 26-8M0204670 -> 8M0204670, 26-76868 -> 76868; склеивай пробел: "
        "88397A 1 -> 88397A1). Если формат номера НЕ описан правилами своего бренда — оставляй номер КАК ЕСТЬ, "
        "НЕ выдумывай и НЕ подгоняй канонику. После приведения убирай дубликаты (одинаковые после нормализации).\n"
        f"{context.article_format_spec}\n\n"
        f"JSON Schema:\n{_STRUCT_SCHEMA_JSON}\n"
    )

# research_rules.md лежит в корне репозитория; в Docker-образ кладётся рядом.
# Путь можно переопределить через RESEARCH_RULES_PATH.
_DEFAULT_RULES_PATH = Path(__file__).resolve().parents[3] / "research_rules.md"
RULES_PATH = Path(os.environ.get("RESEARCH_RULES_PATH", _DEFAULT_RULES_PATH))


# ── Exa-запросы фазы 1 (детерминированные формулировки) ────────────────────────
def build_main_query(article: str) -> str:
    # Keyword-поиск Exa: номер доминирует. Нейронный режим тащил похожие номера,
    # из-за чего точное вхождение не находилось и run уходил в failed_no_data.
    # Единый запрос turn-1: кроссы + модели/применимость + цены на US-магазинах
    # (highlights нередко уже содержат цену; если нет — отдельный price-фолбэк).
    return (
        f"{article} OEM part number cross reference interchange supersedes superseded by "
        "replaces replacement fitment application kit contents "
        "compatible models engines years "
        "price buy for sale in stock USD at US online marine parts stores"
    )


def build_price_query(article: str, hint: str) -> str:
    # Фолбэк: фокусный ценовой запрос, засеянный кратким англ. описанием/брендом
    # (hint, напр. "Mercury Enertia propeller") — поднимает точность товарных страниц.
    seed = " ".join(x for x in [article, (hint or "").strip()] if x)
    return f"{seed} OEM original part price buy for sale in stock USD US online store"


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


def build_difference_query(numbers: list[str]) -> str:
    # Финальный difference-turn: на ПОДТВЕРЖДЁННЫХ кроссах одной детали ищем нюансы
    # (порядок замен, что менялось, границы фита). Упор на genuine/OEM/дилер/форум,
    # aftermarket просим игнорить (он всё равно бан в правилах turn'а).
    nums = ", ".join(numbers)
    return (
        "Genuine OEM / factory parts-catalog supersession notes, manufacturer service "
        "bulletins, and dealer or owner-forum threads about these related OEM part "
        f"numbers for one and the same part: {nums} — plus any newer or older related "
        "OEM numbers not in this list. Focus on genuine factory/OEM parts and OEM "
        "supersession; ignore aftermarket replacement brands and aftermarket "
        "cross-reference listings (such as Sierra, CDI, Dayco, WSM, Caltric). Explain "
        "which OEM number superseded which and the chronological order from newest to "
        "oldest, what was redesigned or changed in the part at each step (material, "
        "shape, mounting, dimensions, internal parts), what to watch out for when using "
        "an older versus a newer version, and which model years or production period "
        "each number covers."
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
- vehicle_classes — массив слагов классов техники из списка выше (можно несколько,
  в т.ч. разных типов — например, общая деталь гидроциклов и снегоходов:
  ["jetski","snowmobile"]). Не смог определить — пустой массив [].
- name/description — на русском; name_en/description_en — ТЕ ЖЕ на английском
  (по англоязычным источникам). Английский ОБЯЗАТЕЛЕН: при непустом name заполни
  name_en, при непустом description — description_en (и так же для компонентов
  kit_contents). Английский кладётся в *_en, а НЕ в скобки русского имени.
- name и name_en — КОРОТКИЕ: из них собирается title фида "<арт1> / <арт2> <name>"
  с жёстким лимитом 50 символов (проверяется, ран отклоняется при превышении).
  Два артикула с пробелами уже съедают ~22 символа, поэтому на САМО имя остаётся
  ~25 символов — держись этого и для name, и для name_en.
  * Пиши ТОЛЬКО тип детали + отличительную спеку (размер/шаг/вариант): напр.
    "Форсунка топливная" / "Fuel injector", "Винт гребной 12.5x19P RH" / "Propeller 12.5x19P RH".
  * НЕ включай в name бренд (он в brand_oem) и модель/двигатель/линейку (она в model) —
    напр. НЕ "Форсунка топливная Ski-Doo 600 E-TEC", а "Форсунка топливная".
  * НЕ дублируй английский в скобках, не пиши "OEM"/маркетинг/длинные расшифровки.
  * Размер/шаг/вращение винта и подобную идентифицирующую спеку НЕ выкидывай —
    ужимай формулировку (сокращения, без лишних слов), пока не влезет в бюджет.
  description/description_en — без ограничения длины, туда выноси все подробности
  (материал, назначение, нюансы), которым не место в коротком name.
- us_prices — массив РОЗНИЧНЫХ цен за ОРИГИНАЛ на US-магазинах для входного артикула.
  Каждый объект: {"site","price","currency","url","article","in_stock","evidence"}.
  * site — домен магазина (напр. "partsvu.com"); url — ссылка на товарную страницу.
  * article — наш OEM-номер, по которому найдена цена (у ритейлеров бывает с префиксом 26-/710-).
  * price/currency — ТЕКУЩАЯ цена продажи (Price/Now) именно нашего номера; НЕ MSRP/зачёркнутая,
    НЕ "you save", НЕ порог доставки ("over $99/$100"), НЕ баллы, НЕ цена другого/substitute товара.
  * Бери только товар В НАЛИЧИИ и только за оригинал (не aftermarket-аналог).
  * Нет валидной цены в источниках — us_prices=[] (дальше отработает отдельный ценовой поиск).
- Пустые строки запрещены: если значения нет — ставь null / пустой массив, не "".
- nuances, supersession — заполняет ТОЛЬКО финальный difference-turn (по своему
  сообщению). До него и когда нюансов нет: nuances=[], supersession=[].
  Не выдумывай их без источника.
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


def format_ebay_hint(listings: list[dict[str, Any]]) -> str:
    """Валидные eBay-объявления по детали для system-prompt (подсказка, не истина).

    Это рыночные US/EN-объявления, прошедшие нашу валидацию соответствия детали.
    Данные продавцов часто врут (ложный OEM, путаница бренда/совместимости) —
    подаём строго как наводку, не как доказательство."""
    lines = [
        "--- СПРАВОЧНАЯ подсказка: валидные eBay-объявления по этой детали — ЭТО НЕ ДОКАЗАТЕЛЬСТВО ---",
        "Это рыночные объявления (US/EN), прошедшие нашу валидацию соответствия детали.",
        "ВНИМАНИЕ: данные продавцов часто НЕВЕРНЫ (ложный OEM, путаница бренда/совместимости,",
        "маркетинг). Используй ТОЛЬКО как наводку (бренд, OEM-номера, назначение, состав) и",
        "подтверждай каждое поле независимым Exa-источником. Ничего отсюда не копируй в результат",
        "вслепую: номер из объявления без подтверждения источником — максимум article_low_confidence.",
        f"объявлений: {len(listings)}",
    ]
    for lst in listings:
        lines.append(f"• eBay item {lst['item_id']}: {lst['title']}")
        for spec in lst["specifics"]:
            lines.append(f"    {spec['name']}: {spec['value']}")
        if lst["description"]:
            lines.append(f"    описание: {lst['description']}")
    lines.append("--- Конец eBay-подсказки (подсказка ≠ доказательство, всё проверяй по источникам) ---")
    return "\n".join(lines)


def build_system_prompt(context: ResearchContext) -> str:
    rules = RULES_PATH.read_text(encoding="utf-8")
    aliases = "\n".join(
        f"  {alias} -> {canon}" for alias, canon in context.brand_aliases.items()
    )
    smart_hint = ""
    if context.smart_payload is not None:
        smart_hint = "\n" + format_smart_hint(context.smart_payload) + "\n"

    ebay_hint = ""
    if context.ebay_listings:
        ebay_hint = "\n" + format_ebay_hint(context.ebay_listings) + "\n"

    classes = "\n".join(
        f"  {vc.slug} — {vc.title_ru}" for vc in context.vehicle_classes
    )
    return f"""\
Ты исследуешь OEM-запчасть по входному артикулу и собираешь о ней структурированные \
данные строго на основании предоставленных источников. Текстовые поля заполняй и \
на русском (name/description), и на английском (name_en/description_en). Дополнительно \
ищи розничные цены за ОРИГИНАЛ на американских интернет-магазинах и заполняй us_prices \
(правила — в блоке формата ниже).

Классы техники (vehicle_classes — слаги только из этого списка):
{classes}

Допустимые Smart-бренды (brand_oem только из этого списка, UPPER_SNAKE_CASE):
  {", ".join(context.allowed_brands)}

Алиасы OEM-брендов → Smart-бренд (нормализуй найденные бренды к правой части):
{aliases}
{smart_hint}{ebay_hint}
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


def build_price_user_message(article: str, raw_json: str, current_json: str) -> str:
    return (
        f"Это ДОПОЛНИТЕЛЬНЫЙ поиск ЦЕН за ОРИГИНАЛ артикула {article} на американских "
        f"магазинах (в основном поиске валидной цены не нашлось).\n\n"
        f"Свежий Exa-поиск (url/title/полный текст товарных страниц):\n{raw_json}\n\n"
        f"Твой текущий JSON:\n{current_json}\n\n"
        "Заполни us_prices: по каждому магазину, где есть товарная страница НАШЕГО номера "
        f"({article}, возможно с префиксом 26-/710-), возьми ТЕКУЩУЮ цену продажи (Price/Now), "
        "не MSRP/зачёркнутую, не порог доставки (over $99/$100), не баллы, не цену другого/"
        "substitute товара; только в наличии и только за оригинал. site = домен магазина, "
        "currency как на странице, article = наш номер. Нет валидной цены — оставь us_prices=[]. "
        "Все остальные поля JSON сохрани БЕЗ изменений. Ответ — только валидный JSON по схеме."
    )


def build_repair_user_message(error_text: str) -> str:
    """Repair-turn: возвращаем агенту ПОЛНЫЙ текст упавшей валидации его последнего
    ответа (сам ответ уже лежит в session-истории) и требуем исправленный ПОЛНЫЙ JSON."""
    return (
        "Твой предыдущий ответ НЕ прошёл валидацию. Полный текст ошибки:\n\n"
        f"{error_text}\n\n"
        "Исправь и верни ЗАНОВО ПОЛНЫЙ JSON-объект по той же схеме — весь объект "
        "целиком, не фрагмент и не диф. Не меняй фактические данные, которых ошибка "
        "не касается; исправь ровно то, на что указывает валидация. "
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


def build_difference_user_message(article: str, raw_json: str, current_json: str) -> str:
    return (
        f"ФИНАЛЬНЫЙ difference-turn по детали с входным артикулом {article}. Набор "
        "confirmed-кроссов уже собран — теперь по ним достаём НЮАНСЫ между номерами.\n\n"
        f"Свежий Exa-поиск (url/title/highlights):\n{raw_json}\n\n"
        f"Твой текущий JSON:\n{current_json}\n\n"
        "ЗАПОЛНИ две вещи (каждая запись — с source_url и evidence-цитатой):\n"
        "1) nuances — список отличий/нюансов: {text, articles, source_url, evidence}. "
        "text — человеческим языком, ЧЕМ отличается/на что смотреть. articles — номера, "
        "к которым нюанс относится: если он про КОНКРЕТНЫЕ номера (например «нужен отдельный "
        "Smart-Lok install kit» — это про тот номер, чья страница это пишет) → перечисли их; "
        "если про ВСЮ деталь (например «wet-joint и dry-joint не взаимозаменяемы», «E-coated — "
        "только пресная вода») → articles=[]. Примеры text: «у новых металлические шайбы вместо "
        "пластиковых», «Gen 2 — другие внутренние шестерни», «нужна дилерская перепрошивка», "
        "«крепёжный кит НЕ входит, заказывается отдельно (Order Separate)».\n"
        "   НЮАНС = только СОДЕРЖАТЕЛЬНЫЙ факт. НЕ пиши как нюанс:\n"
        "   - мета-комментарии о поиске: «источник не формулирует замену», «порядок supersession "
        "не задан», «данных не найдено» — если отличий нет, оставь nuances=[] (это нормальный итог);\n"
        "   - общие советы без факта: «сверяйте спецификацию перед покупкой» и т.п.;\n"
        "   - ШАБЛОННЫЕ ДИСКЛЕЙМЕРЫ продавцов: «functional replacement, may look different from "
        "the original», «check fitment before purchase» и подобное — магазины пишут это на каждой "
        "странице замены, информации о конкретной детали тут нет. Пиши нюанс, только если источник "
        "говорит, ЧТО ИМЕННО изменилось;\n"
        "   - ПРИМЕНЯЕМОСТЬ (список моделей/годов/HP/скорости) — это поле models, не нюанс. В нюанс "
        "из применяемости идёт только ОТЛИЧИТЕЛЬНЫЙ признак варианта — то, без чего не выбрать "
        "между номерами (например «для моторов БЕЗ навесной помпы забортной воды»), без "
        "перечисления остальных характеристик;\n"
        "   - «в комплекте идёт X (Included)» — это СОСТАВ набора (is_kit/kit_contents), не нюанс. "
        "Нюансом пиши обратное: к какому-то номеру комплект НЕ прилагается / «Order Separate».\n"
        "   РАЗВИЛКА = СРАВНЕНИЕ: если один старый номер заменён НЕСКОЛЬКИМИ (или в семье "
        "параллельные варианты) — сформулируй ОДИН сравнительный нюанс «X — вариант с …; Y — "
        "вариант для …; выбирать по …» с articles=[все участники], а НЕ отдельные описания "
        "каждого номера. Ответ на вопрос «какой из них брать?» — самое ценное.\n"
        "   Если источники ПРОТИВОРЕЧАТ друг другу по спецификации — пиши это прямо как факт "
        "(«официальный каталог Mercury: диаметр 12.8\", дилеры пишут 12.5\"»), а не как совет.\n"
        "   КАК ПИСАТЬ text — как объяснение человеку, который не знает термина:\n"
        "   - технический термин РАСШИФРУЙ по-русски, оригинал можно в скобках: не «suits external "
        "circlip type universals», а «крестовина крепится стопорными кольцами СНАРУЖИ, на чашках "
        "(external circlip); у ранних приводов кольца внутри ушей вилки»;\n"
        "   - назови ПРАКТИЧЕСКОЕ СЛЕДСТВИЕ: что встанет/не встанет и куда («на ранние Bravo с "
        "внутренними кольцами не подойдёт»);\n"
        "   - если источник даёт ПРИЗНАК ПРОВЕРКИ (визуальный признак, серийный номер, год) — "
        "включи его в text: «кольца видны снаружи на чашках → поздний тип; серийник 0M111208 и "
        "выше». Не выдумывай признак, которого нет в источнике.\n"
        "2) supersession — рёбра порядка замен {newer, older, source_url, evidence}, новое→старое. "
        f"ТОЛЬКО среди уже подтверждённых номеров детали (из numbers.article вокруг {article}). "
        "НЕ вводи в порядок НОВЫЙ номер, которого нет в numbers.article (особенно из тюнинг-"
        "магазинов вроде Weddle) — если видишь «возможно есть новее, номер X», напиши это как "
        "nuance с articles=[], а в supersession его НЕ клади.\n\n"
        "ИСТОЧНИКИ (жёстко):\n"
        "- AFTERMARKET ЗАПРЕЩЁН полностью — ни кросс, ни нюанс, ни порядок. Грубо распознавай "
        "aftermarket по бренду ДЕТАЛИ (Sierra, CDI, Barr, GLM, Osco, EMP, WSM, Caltric, Dayco, "
        "Kimpex, Mallory, Weddle) или само-пометке «aftermarket/replacement». Гейт по бренду "
        "ДЕТАЛИ, не по магазину: genuine Mercury/Quicksilver/BRP/Yamaha с любого магазина — ок.\n"
        "- OEM/каталог/дилер — твёрдый пруф; форум — мягкий (в одиночку не двигает номер в irrelevant).\n"
        "- Нет пруфа из разрешённого источника — НЕ пиши нюанс (молча пропусти).\n\n"
        "РАСКЛАДКА НОМЕРОВ (правило «в numbers.article нет плохих номеров»):\n"
        f"- ЯКОРЬ: входной артикул {article} ВСЕГДА остаётся в numbers.article — не трогай его.\n"
        "- Если confirmed-номер ДОКАЗАННО (OEM/дилер-пруф) оказался ДРУГОЙ деталью (например "
        "dry-joint против нашего wet-joint) → перенеси его в numbers.irrelevant с why_irrelevant.\n"
        "- Если только ПОДОЗРЕНИЕ что чужой, без твёрдого OEM-пруфа (или только форум) → перенеси "
        "в numbers.article_low_confidence с why_low_confidence (буфер сомнения).\n"
        "- Чистый, та же деталь → остаётся в numbers.article.\n\n"
        "Не выдумывай номера/нюансы без источника. Все ОСТАЛЬНЫЕ поля JSON сохрани без изменений. "
        "Один номер — только в одном массиве numbers.*. Ответ — только валидный JSON по схеме."
    )
