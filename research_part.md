# `research_part.py` — спецификация

`research_part.py` — отладочный скрипт, который прогоняет research-pipeline по одному артикулу. Цель — проверить контракт `StructuredResult`, поведение модели и работоспособность связки `Agents SDK + strict JSON schema + Pydantic + Exa` без БД и worker-пула.

Скрипт реализует то же поведение research-агента, что описано в `PARTS_RESEARCH_SPEC.md` (разделы «Research-агент», «Слои валидации», «Структура итогового JSON»). Тут собрана только специфика прототипа и то, что отличает его от production-сборки.

## Что прототип делает

- Принимает на вход один артикул (CLI-аргумент или дефолт `76868A04`).
- Прогоняет фазу 1 (1–3 turn'а, backend-driven Exa) и фазу 2 (свободный поиск с function-тулами).
- Печатает итоговый JSON в stdout.

## Что прототип не делает (по сравнению с production)

- Никакой БД `parts_research`: `task_runs.result_json`, `agent_history`, `agent_stream_events`, `exa_cache`, `exa_cache_usage`, `plugin_payloads`, `draft_*` не задействуются.
- Тулы фазы 2 — `@function_tool`, исполняемые в том же процессе. В production-системе MCP-серверов тоже нет (см. спеку, раздел «Архитектура процессов») — оба агента используют `@function_tool`.
- Никаких файлов на диске.
- Нет worker-пула и очереди.
- Нет PostgresSession — история turn'ов держится в памяти через `result.to_input_list()`.
- Нет Smart-плагина — выжимка Smart-данных в системный промпт не подмешивается.
- Нет подключений к `smart_test`/`brands_mapping`: списки `Smart.brands`, `Smart.vehicle_classes` и `brand_aliases` **захардкожены** в коде. Над каждым блоком — комментарий: «в production берётся из БД через FDW».

## Запуск

```text
python research_part.py 76868A04
```

Зависимости (см. `pyproject.toml`) ставятся в глобальный mise-python:
```text
python -m pip install -e .
```

Переменные окружения (из `.env`):

- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL_RESEARCH` — параметры эндпоинта и модели.
- `EXA_API_KEY` — для прямых вызовов exa-py.

`SMART_DATABASE_URL` и `BRAND_MAPPING_DATABASE_URL` в прототипе **не используются** — они нужны для production-сборки.

## Алгоритм пошагово

### 1. Старт

- Загрузка `.env` через `python-dotenv`.
- `set_tracing_disabled(True)` — отключаем телеметрию Agents SDK на openai.com (у нас свой эндпоинт).
- Чтение env-переменных, артикула из `sys.argv[1]`.

### 2. Pre-validation артикула

- `article = raw.strip().upper()`.
- Проверка регуляркой `^[A-Z0-9\-]+$` (как в спеке, раздел «Валидация артикула на входе»).
- На провал — `ValueError`, exit 2.

### 3. Контекст промпта (хардкод)

В коде объявлены три константы с комментариями «в production — из БД»:

- `ALLOWED_BRANDS` — список Smart-брендов (`ARCTIC_CAT, AUDI, BRP, HONDA, LAND_ROVER, MERCEDES_BENZ, MERCRUISER, OMC, POLARIS, SEASTAR, SUZUKI, VOLVO, YAMAHA`).
- `VEHICLE_CLASSES` / `ALLOWED_VEHICLE_CLASSES` — классы техники (boat, jetski, quad, snowmobile, motorcycle, auto) с русскими названиями.
- `BRAND_ALIASES` — словарь алиасов (`Mercury → MERCRUISER`, `Quicksilver → MERCRUISER`, `Sea-Doo → BRP`, и т.д.).

Содержимое `research_rules.md` читается с диска и подмешивается в системный промпт целиком.

### 4. Системный промпт

Один на весь run. Собирается из:

- Преамбула «ты исследуешь OEM-запчасть».
- Список классов техники (vehicle_classes: slug — название).
- Список allowed Smart-брендов.
- Список алиасов брендов **обычным текстом** (без markdown-таблицы, без визуального выравнивания).
- Текст `research_rules.md`.
- Блок `SCHEMA_REMINDER` — пример валидного JSON и жёсткие правила по структуре (`kit_contents` — массив объектов, `article: string | null`, без поля `key`).

### 5. Создание агента (фаза 1)

```text
client  = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
model   = OpenAIChatCompletionsModel(model=LLM_MODEL_RESEARCH, openai_client=client)
agent_1 = Agent(
    name="research-phase1",
    instructions=system_prompt,
    model=model,
    output_type=StructuredResult,
    model_settings=ModelSettings(retry=ModelRetrySettings(max_retries=2, policy=retry_policies.network_error())),
)
```

`output_type=StructuredResult` включает strict JSON schema (см. слой 1 валидации в спеке). У агента фазы 1 **нет тулов**.

`AsyncOpenAI` создаётся **без перезаписи** `timeout`/`max_retries` — дефолтов openai SDK достаточно (`read=600s`, `max_retries=2`).

**Retry поверх стрима.** Встроенный `ModelSettings.retry` не повторяет обрывы посреди стрима (replay-safety). Поэтому каждый turn запускается через хелпер `_run_and_capture`, который сам повторяет turn целиком (до 2 раз) при транспортной ошибке: `httpx.ReadError`/`RemoteProtocolError`/`ConnectError`/`ReadTimeout`, `openai.APIConnectionError`/`APITimeoutError` и «голый» `openai.APIError` (`stream error … INTERNAL_ERROR` от HTTP/2 RST_STREAM). `APIStatusError` (4xx/5xx), `MaxTurnsExceeded` и ошибки валидации в retry **не** попадают.

### 6. Фаза 1, Turn 1 — основной Exa

- Backend сам формирует основной поисковый запрос (детерминированная формулировка по `research_part.ts`-оригиналу) и зовёт `exa.search(query, num_results=10, contents={"highlights": True})`.
- Из `SearchResponse` берётся только `r.results`. Каждый `Result` сериализуется в компактный dict напрямую через атрибуты dataclass: `{url, title, highlights}`. Все остальные поля (`image`, `favicon`, `score`, `subpages`, `extras`, `entities`, `crawl_date`, `published_date`, `author`, `text`, `summary`) — не нужны, не подмешиваются.
- **Substring-check без нормализации**: `article.lower() in json.dumps(payload).lower()`. На провал — `NoExactDataError`, exit 2 (= `failed_no_data`). Это знакомое ограничение для артикулов с дефисами, по которым Exa нормализует представление иначе.
- User-message turn'а 1: «Входной артикул X, основной Exa-поиск ниже, сформируй стартовый JSON по схеме» + raw_exa блок.
- `Runner.run_streamed(agent_1, input=user_message)`. Drain событий (минимальный лог в stderr: `reasoning`/`tool_called`/`message_output_created`).
- `current = streamed.final_output` (уже instance `StructuredResult` — strict schema гарантирует структуру).
- `post_validate(current, expected_part_number=article, allowed_brands=..., allowed_vehicle_classes=...)` — слой 3.

### 7. Фаза 1, Turn 2 — low_confidence (если есть)

Запускается, если `current.numbers.article_low_confidence` непуст.

- Backend формирует low-confidence-запрос и зовёт Exa.
- `history = last_result.to_input_list()` — полная переписка предыдущего turn'а.
- В новый user-message **явно** включается:
  - свежий компактный Exa-результат;
  - **`current.model_dump_json(indent=2)`** прямо в тексте — страховка от потери непустых полей;
  - инструкция «обнови распределение артикулов по `article` / `article_low_confidence` / `irrelevant`, остальные поля сохрани без изменений; не повторяй один артикул в двух массивах».
- `Runner.run_streamed(agent_1, input=history)`. Drain.
- `current = streamed.final_output`. `post_validate`.

### 8. Фаза 1, Turn 3 — kit_contents (если is_kit)

Запускается, если `current.is_kit == True`.

- Backend формирует kit-contents-запрос по подтверждённым артикулам и зовёт Exa.
- Substring-check на raw-ответ (как в Turn 1, тот же без нормализации).
- `history = last_result.to_input_list()`. User-message: «текущий JSON, новый Exa, обнови `kit_contents` (массив объектов; `article: string | null`); остальные поля сохрани; никогда не клади собственный артикул задачи или артикул из `numbers.article` как компонент».
- `Runner.run_streamed`. Drain. `post_validate`.

### 9. Фаза 2 — свободный поиск с тулами

Запускается **всегда** после фазы 1.

- Создаётся **второй агент** с тем же `instructions`, `model`, `model_settings`, `output_type`, но **с тулами**:
  - `web_search_exa(query: str, num_results: int = 10) -> str` — обёртка над `exa.search(...)`, возвращает JSON-сериализованный pick `{url, title, highlights}`.
  - `web_fetch_exa(urls: list[str], max_characters: int = 3000) -> str` — обёртка над `exa.get_contents(urls, text=True)`, возвращает JSON-сериализованный pick `{url, title, text}`, каждое `text` обрезается до `max_characters`.
- Оба тула декорированы `@function_tool`. JSON-schema их аргументов SDK генерирует автоматически из аннотаций типов.
- Внутри тулов — **счётчик** Exa-вызовов в closure (`{"count": 0, "limit": 10}`). Один tool_call = один шаг. На лимите тул возвращает строку «Лимит Exa-вызовов исчерпан, финализируй JSON».
- Никакого backend-substring-check в фазе 2: модель сама контролирует релевантность через инструкцию в промпте.
- User-message фазы 2:
  - текущий JSON (как в фазе 1 turn 2/3);
  - инструкция «у тебя есть `web_search_exa` и `web_fetch_exa`, лимит 10 вызовов; используй ТОЛЬКО источники, в которых явно встречается артикул задачи X; если есть пустые/сомнительные поля — попробуй закрыть; когда удовлетворён — отвечай ТОЛЬКО валидным JSON по схеме».
- `Runner.run_streamed(agent_2, input=history, max_turns=12)`. Drain.
- `current = streamed.final_output`. `post_validate`. Если упёрлись в `max_turns` без финального текста — `MaxTurnsExceeded`, exit 2 (= `failed_validation`).

### 10. Финал

- Печатается `current.model_dump_json(indent=2)` в stdout.
- exit 0.

## Контракт `StructuredResult`

Полностью соответствует разделу «Структура итогового JSON» в `PARTS_RESEARCH_SPEC.md`. Здесь — только специфика прототипа:

- `kit_contents` — `list[KitComponent]` без поля `key` (как в спеке).
- Pydantic-валидаторы в коде: `min_length=1` на всех строковых полях (`task_part_number`, `name` если не null, `weight.source_url`, `weight.evidence`, `models.text`, элементы `models.source_urls`, `article` в `ArticleItem` и наследниках, `kit_name`, `source_url`/`evidence` компонентов и `part_of_kits`).

## Три слоя валидации в коде

Соответствие с разделом «Слои валидации» в спеке:

1. **OpenAI strict JSON schema** — выставляется автоматически Agents SDK при `output_type=StructuredResult`. Гарантирует структуру, обязательность полей, `additionalProperties=false`.
2. **Pydantic** — запрет пустых строк через тип `NonEmptyStr = Annotated[str, AfterValidator(_nonempty)]` (а не `Field(min_length=1)`, чтобы `minLength` не утёк в JSON-схему) и `@model_validator` на `ModelsBlock`, проверяющий непустоту массива `source_urls`. Других проверок здесь нет (всё что в strict-схеме — там, всё что доменное — в `post_validate`).
3. **`post_validate`** — Python-функция, runtime-доменные правила:
   - `task_part_number == expected_part_number`.
   - `brand_oem ⊂ allowed_brands` (хардкод-список).
   - `vehicle_classes ⊆ allowed_vehicle_classes`, без дублей (кросс-типовый мультикласс легален).
   - `is_kit == False ⇒ len(kit_contents) == 0`.
   - `task_part_number` присутствует в `numbers.article`.
   - Артикул не встречается одновременно в нескольких массивах `numbers.*`.
   - `kit_contents[i].article != task_part_number` и не равен любому из `numbers.article`.

Никаких дублирующих проверок между слоями.

## Обработка ошибок и exit codes

| Категория | Exit | Соответствие в production |
|---|---|---|
| `NoExactDataError` (substring-check провал в фазе 1) | 2 | `failed_no_data` |
| `ValueError` из `post_validate` или pre-validation артикула | 2 | `failed_validation` |
| `pydantic.ValidationError` при парсинге | 2 | `failed_validation` |
| `MaxTurnsExceeded` в фазе 2 | 2 | `failed_validation` |
| Сетевые ошибки после retry (`openai.APIConnectionError`, «голый» `openai.APIError` stream error, `httpx.ReadError`) | 1 | `failed_crashed` |

Все ошибки печатают `[error] <Type>: <msg>` в stderr.

## Известные ограничения

- **Substring-check на дефисированных артикулах**: артикулы, по которым Exa нормализует дефисы иначе (`6N7-15A60-00`, `91-805222`, `27-99713`), могут попадать в `failed_no_data`. По наблюдениям ~30–50% дефисированных артикулов. Нормализация дефисов в защитной проверке backend'а не делается — это доменная логика.
- **Сетевые обрывы стрима**: ~7% turn-вызовов рвутся посередине (`httpx.ReadError` или `openai.APIError: stream error … INTERNAL_ERROR`). Встроенный SDK-retry такие mid-stream обрывы не забирает (replay-safety) — их повторяет собственный `_run_and_capture`-loop (см. шаг 5).
- **Strict JSON schema**: модель `cursor-gpt55(high)` на нашем эндпоинте поддерживает `response_format=json_schema, strict=true`. Если эндпоинт поменяется — потребуется fallback на plain JSON-парсинг.

## Связанные документы

- `PARTS_RESEARCH_SPEC.md` — полная спека системы (разделы «Research-агент», «Слои валидации», «Структура итогового JSON», «Документация и ссылки»).
- `research_rules.md` — жёсткие правила работы агента (OEM/Mercury/SKU/kit/weight). Подмешиваются в системный промпт.
- `IMPLEMENTATION_PLAN.md` — план перехода от прототипа к production.
