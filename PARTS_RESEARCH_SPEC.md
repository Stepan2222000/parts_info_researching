# Спецификация системы ресерча запчастей

## Цель

Мы делаем систему, которая принимает артикулы запчастей, глубоко ищет по ним информацию в интернете, сохраняет весь процесс ресерча в отдельной рабочей базе, а в каталожную Smart-базу записывает только аккуратные итоговые результаты.

Главная граница такая:

- `parts_research` — рабочая messy-база ресерча. Тут хранится все: задачи, запуски, итоговые JSON-результаты, распарсенные draft-данные, источники, evidence, Exa-кэш, действия агентов, выполненные SQL, сессии куратора, история сообщений агентов, события стрима, данные от source-плагинов.
- `smart_test` — каталожная Smart-база. Тут хранятся только обычные каталожные записи: запчасти, бренды, наборы, компоненты, связи между ними, draft/unverified-флаги.
- `brands_mapping` — отдельная reusable база для маппинга OEM-названий брендов в Smart-бренды. Подключается через FDW. Используется и в этом проекте, и в других проектах.

`parts_research` может содержать спорные, неполные и промежуточные данные. Это нормально, потому что это рабочая база.

`smart_test` должна оставаться максимально чистой по форме. Там могут быть draft-записи, но они должны быть похожи на реальные каталожные записи, а не на мусор от ресерча.

Система не должна придумывать данные. Если факт не найден, поле остается `null`, пустым или draft, в зависимости от смысла и ограничений Smart-схемы.

## Общая логика работы

Обычный поток такой:

1. Пользователь добавляет один или несколько артикулов в очередь.
2. Backend нормализует и валидирует артикулы. Если артикул не проходит валидацию — он не принимается в очередь, отдается жесткая ошибка. Дополнительно перед постановкой в очередь backend через FDW проверяет, что артикул не финализирован в Smart.
3. Для каждого артикула создается отдельная задача (`task`).
4. Worker пуллит задачи из очереди и стартует под каждую отдельный run.
5. На каждый run поднимается отдельная сессия research-агента (PostgresSession в `agent_history`).
6. Research-агент работает в две фазы. Фаза 1 — backend-driven: backend сам делает обязательные Exa-запросы, передаёт сырые результаты модели, модель только структурирует ответ в JSON. Фаза 2 — agent-driven: модель получает Exa-тулы и сама дозаполняет пробелы.
7. Финальный JSON сохраняется в БД (`task_runs.result_json`), весь поток событий — в `agent_stream_events`. Никаких файлов на диске.
8. Backend детерминированно парсит итоговый JSON в draft-таблицы `parts_research`.
9. Curator/write-agent запускается только когда пользователь явно просит его обработать очередь в чате.
10. Curator смотрит draft-данные, evidence, Smart через FDW, brand_mapping через FDW, при необходимости сам вызывает Exa и сам выполняет SQL.
11. Curator записывает аккуратные draft-результаты в Smart батчами.
12. Человек позже вручную проверяет записи в Smart и снимает draft/unverified-флаги.

Никаких файлов и каталогов на диске не используется. Всё состояние системы — в Postgres.

## Базы данных

### `parts_research`

`parts_research` — основная база системы. В ней хранится все, что связано с процессом ресерча:

- задачи по артикулам и их статусы;
- отдельные запуски задач (`runs`), история перезапусков сохраняется целиком;
- финальный JSON каждого run'а как JSONB-поле;
- история input-items для Agents SDK Session (`agent_history`);
- поток высокоуровневых streaming-событий каждого run'а (`agent_stream_events`);
- кэш Exa-запросов (raw response целиком в БД);
- связь run ↔ Exa-вызовы с указанием фазы (`exa_cache_usage.phase`);
- распарсенные draft-запчасти и draft-компоненты в виде нормальных реляционных таблиц;
- данные от source-плагинов;
- лог SQL, который выполнял curator;
- сессии куратора и сообщения в чате с куратором;
- публикации в Smart (что и из какого run'а попало в каталог).

Эта база должна быть удобной для анализа. Curator может смотреть ее обычным SQL и понимать, что уже найдено, что спорное, что записано в Smart, а что еще только draft.

### `smart_test`

`smart_test` — тестовая Smart-база для каталожных результатов. На практике сейчас используем именно ее. Перехода в продовый Smart этой системой не делаем — это вне scope.

Актуальная Smart-схема (см. `central_smart_logic/main.sql` + миграции):

- `parts` — запчасти и наборы. Важные поля: `id` (генерится сам как `smart_XXXXXXXX`), `name`, `articles TEXT[]` (валидируется regex `^[A-Z0-9\-]{4,20}$`, без дублей внутри массива, для опубликованных записей обязателен минимум один артикул), `description`, `product_type` (FK на `product_types(name)`), `model`, `weight_kg NUMERIC(8,3)`, `is_draft`.
- `brands(name PK)` — справочник Smart-брендов в UPPER_SNAKE_CASE. Точный список не хардкодим в коде — грузим из `smart.brands` через FDW при старте каждого run'а и подмешиваем в system-prompt research-агента.
- `part_brands(part_id, brand)` — M:N связка. Для `is_draft = false` минимум один бренд обязателен.
- `part_articles(article PK, part_id)` — глобально-уникальный реестр обычных артикулов, синхронизируется триггером.
- `part_components(parent_id, child_id, quantity, can_be_sold_separately, is_unverified)` — состав наборов, циклы запрещены триггером.
- `product_types(name PK)` — фиксированные значения: `Для автомобилей`, `Для мототехники`, `Для водного транспорта`.
- `parts_with_components` — view с разворотом компонентов и вычисленным `is_kit` через `EXISTS`.

В Smart мы не храним raw Exa-ответы, длинные evidence-тексты, логи агента и историю research-JSON. Smart хранит итоговую каталожную форму.

Отдельной колонки `is_kit` в Smart нет и не нужно. Набор определяется по наличию строк в `part_components`, где запчасть является `parent_id`.

### `brands_mapping`

`brands_mapping` — отдельная база на хосте `194.164.245.107:5411`, db `brands_mapping`. Это reusable справочник, который будет использоваться и в других проектах.

В ней лежит таблица `brand_aliases(alias TEXT PRIMARY KEY, canonical TEXT NOT NULL)`. Значения `canonical` всегда соответствуют названиям из Smart `brands.name`.

Таблица заполняется руками одноразовыми INSERT'ами. Полный набор живёт прямо в БД, не в коде — это растущая таблица.

FK на Smart `brands` здесь нет, потому что это разные базы. Согласованность гарантируется тем, что curator перед записью бренда в Smart проверяет, что `canonical` действительно есть в `smart.brands`, либо это видно через FDW.

При добавлении нового бренда в Smart мы вручную добавляем нужные алиасы в `brand_aliases`. Никакой автоматики здесь нет — таблица маленькая.

## FDW

`parts_research` видит `smart_test` и `brands_mapping` через PostgreSQL FDW (`postgres_fdw`). Это нужно, чтобы curator из одной SQL-сессии мог:

- читать research-таблицы напрямую;
- читать и писать Smart-таблицы (`INSERT`/`UPDATE`/`DELETE` поддерживаются postgres_fdw);
- читать и обновлять brand_aliases;
- сравнивать draft с уже записанными Smart-данными;
- делать обычные JOIN-ы между research, Smart и brand_mapping.

Структура подключения:

- расширение `postgres_fdw`;
- foreign server `smart_fdw` → `smart_test`, схема локально импортируется как `smart`, с `batch_size=50` для эффективной записи через FDW;
- foreign server `brand_mapping_fdw` → `brands_mapping`, схема как `brand_mapping`;
- user mapping для пользователя приложения.

Пример:

```sql
select *
from smart.parts
where '807252T5' = any(articles);

select canonical
from brand_mapping.brand_aliases
where alias = 'Quicksilver';
```

Smart через FDW также считается одним из источников контекста (см. Smart-плагин ниже).

## Очередь и параллелизм

Очередь простая, FIFO, без приоритетов. Реализуется обычной таблицей `task_runs` со статусом, без отдельного workflow-движка и без Redis/BullMQ.

Worker пуллит свободные задачи через `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` внутри транзакции, помечает `running` и тут же атомарно отпускает блокировку. Параллелизм — 30 одновременно выполняющихся research-runs (управляется через `asyncio.Semaphore`). Дополнительные задачи лежат в `queued` и ждут.

### Защита на этапе постановки в очередь

Перед `INSERT INTO tasks` submit-команда обязательно делает проверку через FDW:

```sql
SELECT 1 FROM smart.parts WHERE $1 = ANY(articles) AND is_draft = false;
```

Если такая запись есть — артикул считается уже финализированным человеком, постановка в очередь отказывается с понятной ошибкой («article X is already finalized in Smart, research skipped»). Это самая ранняя точка защиты: бессмысленно гонять research, если результаты всё равно нельзя записать в Smart.

Если запись в Smart есть, но `is_draft = true` — постановка в очередь разрешена; curator позже сможет дополнить пустые поля.

Если записи нет — постановка нормальная.

Статусы run'а:

- `queued` — задача в очереди, ждет worker'а;
- `running` — worker начал выполнение pipeline'а;
- `done` — итоговый JSON получен, провалидирован, draft-таблицы заполнены;
- `failed_no_data` — Exa не нашел источников с точным артикулом в основном поиске фазы 1, либо найден только aftermarket, либо вес есть, но единицы не распознаны. Это не баг, просто данных нет. В Smart ничего не пишется;
- `failed_validation` — модель отдала невалидный JSON в одном из turn'ов pipeline'а, либо в фазе 2 модель упёрлась в `max_turns` без финализации. Raw payload сохраняется для отладки. Авто-ретраев нет, пользователь перезапускает руками;
- `failed_crashed` — worker упал во время выполнения. Восстанавливается так: при старте worker помечает все `running`-задачи старше 30 минут как `failed_crashed`. Heartbeat пока не делаем;
- `needs_human_review` — задача дошла до финального JSON, но это пограничный случай, нужен ручной взгляд (kit без состава, не определён product_type, и т.п.).

`needs_human_review` нужен для случаев, когда система фактически собрала данные, но не имеет права автоматически их публиковать. Эти задачи не failure — они просто ждут человека.

Aftermarket-only находки, weight в нераспознанных единицах, провал валидации — это НЕ `needs_human_review`, это `failed_no_data` или `failed_validation`.

Перезапуск задачи (`re-run`) создает новый run с новым `run_id`. Старые runs остаются в БД и видны как история. Draft-таблицы всегда привязаны к конкретному run'у. Curator при работе смотрит только последний `done`-run по задаче, но может через SQL заглянуть и в старые, если нужно.

## Стек технологий

End-to-end Python.

- **Python 3.13**, async-first.
- **`uv`** — пакетный менеджер. Проект объявляется в `pyproject.toml`, lock-файл `uv.lock`.
- **`openai-agents`** — основной фреймворк агентов (Agents SDK от OpenAI). Используется и для research-агента, и для куратора.
- **`openai`** — официальный клиент. Используется внутри Agents SDK для общения с эндпоинтом через `AsyncOpenAI + OpenAIChatCompletionsModel`.
- **`asyncpg`** — async-драйвер Postgres. Все обращения к БД через пул соединений (default `min_size=10, max_size=60`).
- **`pydantic` v2** — валидация финального JSON модели.
- **`mcp`** (официальный `mcp.server.fastmcp.FastMCP`) — MCP-сервер для куратора. Использует Streamable HTTP transport, `stateless_http=True, json_response=True`.
- **`python-dotenv`** — чтение `.env`-файлов.
- **PostgreSQL 18** — все три БД (`parts_research`, `smart_test`, `brands_mapping`) уже подняты на сервере как отдельные контейнеры; новые БД не поднимаем. `postgres_fdw` для связи.

Никаких Node.js, npm, TypeScript, Codex SDK в системе нет. Никакого mihomo HTTP-proxy: эндпоинт LLM ходит напрямую. Никакого `~/.codex/auth.json`: авторизация — обычный Bearer-токен в заголовке.

## LLM-эндпоинт

Используется OpenAI-совместимый Chat Completions эндпоинт:

- Base URL: `http://194.164.245.107:8317/v1`
- API key: `local-gpt55` (Bearer-токен)
- Модель research-агента: `cursor-gpt55(high)` (gpt-5.5 с high reasoning)
- Модель куратора: `cursor-gpt55(high)`

Параметры берутся из env-переменных (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL_RESEARCH`, `LLM_MODEL_CURATOR`), чтобы менять без правки кода.

Подключение к эндпоинту делается через стандартный путь Agents SDK: `AsyncOpenAI(base_url, api_key)` → `OpenAIChatCompletionsModel(model, openai_client)` → `Agent(..., model=...)`.

Tracing Agents SDK отключаем (`set_tracing_disabled(True)`), потому что эндпоинт не openai.com.

## Архитектура процессов

Система состоит из двух долгоживущих процессов (плюс уже существующие Postgres-контейнеры на сервере):

### Process 1 — `parts_research_worker`

Основной долгоживущий процесс. Содержит:

- Pipeline research-агента целиком (фаза 1 + фаза 2).
- Пул соединений к `parts_research`.
- Цикл "взять следующую `queued`-задачу через `FOR UPDATE SKIP LOCKED` → выполнить pipeline → пометить готовой/ошибочной".
- `asyncio.Semaphore(30)` для ограничения параллелизма.
- При старте: чистит зависшие `running`-задачи старше 30 минут (`failed_crashed`).

Тулы research-агента в фазе 2 (`web_search_exa`, `web_fetch_exa`) реализованы как обычные Python-функции с `@function_tool` Agents SDK. Они выполняются прямо в процессе worker'а. MCP-обёртка для них не используется — она избыточна, так как research-агент и его тулы живут в одном процессе.

### Process 2 — `parts_research_curator_mcp`

HTTP-сервер на `mcp.server.fastmcp.FastMCP` со Streamable HTTP transport. Висит на фиксированном порту (по умолчанию 8765).

Регистрирует тулы куратора:
- `execute_sql({sql})` — сырой SQL по `parts_research` + FDW. Логируется в `agent_sql_log`.
- `save_to_smart({parts: [...]})` — batch-публикация в Smart. См. `save_to_smart.md`.
- `mark_needs_review({run_id, reason})` — пометить run как `needs_human_review`.
- `web_search_exa({query, num_results})` — Exa-поиск через тот же кэширующий клиент.
- `web_fetch_exa({urls, max_characters})` — Exa-fetch через тот же кэширующий клиент.

Из HTTP-заголовков читает `X-Curator-Session-Id` (для логирования действий в правильную сессию). Поскольку MCP-сервер обслуживает только куратора, отдельный `X-Run-Id` здесь не нужен — у куратора нет привязки к одному run'у.

### Короткоживущие команды

- `python -m parts_research.cli.submit ARTICLE [ARTICLE ...]` — кладёт артикулы в очередь.
- `python -m parts_research.cli.research ARTICLE` — гонит один артикул через pipeline без worker'а (отладка).
- `python -m parts_research.cli.curator` — открывает CLI REPL куратора (для этапа 3 до появления UI).

## Research-агент

Research-агент работает в **две фазы**. Фаза 1 — детерминированная, ведёт backend. Фаза 2 — агентская, ведёт модель.

### Общий setup (до первого вызова модели)

Backend параллельно подгружает контекст:

1. Список Smart-брендов (для валидации `brand_oem`).
2. Список Smart `product_types`.
3. `brand_mapping.brand_aliases` через FDW.
4. Smart-plugin payload (точное совпадение по артикулу + связанные `part_components` parents/children).

Также читается `research_rules.md` — жёсткие правила OEM/Mercury/kit/нормализации.

Из всего этого собирается **системный промпт** агента. Он одинаковый для всех turn'ов одного run'а:

- общие правила (`research_rules.md`),
- допустимые product_type,
- допустимые Smart-бренды,
- таблица алиасов брендов (markdown),
- Smart-подсказка (если есть точное совпадение по артикулу),
- описание желаемого JSON-формата и схемы,
- инструкция: «модель отвечает ТОЛЬКО валидным JSON по схеме, без markdown-обёртки, без комментариев, без лишнего текста».

Создаётся **PostgresSession** с `session_id = f"research_run_{run_id}"`. Это реализация Session-протокола Agents SDK поверх таблицы `agent_history` (методы `get_items`, `add_items`, `pop_item`, `clear_session`).

### Фаза 1 — детерминированный pipeline (1–3 turn'а)

В фазе 1 у модели **нет никаких тулов**. Она получает порцию данных и обязана ответить чистым JSON в текстовом сообщении.

#### Фаза 1, Turn 1 — основной Exa-поиск

1. Backend сам делает Exa-вызов `web_search_exa({query, numResults=10})` с детерминированной формулировкой запроса по артикулу. Вызов проходит через кэширующий слой: hash от tool_name + canonical JSON args → SELECT в `exa_cache`; если попадание — берём оттуда (`exa_cache_usage.hit=true`), иначе зовём реальный Exa MCP (`https://mcp.exa.ai/mcp`), сохраняем (`exa_cache_usage.hit=false`). В обоих случаях `exa_cache_usage.phase = 'main'`.
2. Backend проверяет, что в raw-ответе есть точное вхождение артикула (substring-match по нижнему регистру в JSON-сериализации). Если нет — run помечается `failed_no_data`, pipeline останавливается.
3. Backend создаёт **агента без тулов** и составляет первое user-сообщение, в которое включается:
   - полный raw Exa-ответ (без обрезок);
   - инструкция: «вот основной поиск по артикулу X, сформируй стартовый JSON по схеме».
4. Запускается `Runner.run_streamed(agent, input=user_msg, session=session)`. Все события стрима пишутся в `agent_stream_events(run_id, turn_idx=1, seq, event)`.
5. По завершении: парсим JSON из последнего assistant-сообщения, валидируем pydantic-моделью, на провале — `failed_validation`, на успехе — записываем в `task_runs.result_json`.

#### Фаза 1, Turn 2 — low_confidence-проверка (если есть)

Запускается, только если в текущем `result_json` массив `numbers.article_low_confidence` непуст.

1. Backend делает Exa-вызов с детерминированной формулировкой «проверь эти артикулы как OEM-кроссы для исходного X». `phase='low_confidence'`.
2. Формируется новое user-сообщение. В него явно включается:
   - свежий Exa-ответ (raw JSON);
   - **текущий JSON модели** (целиком, прямо в тексте) — как страховка от потери полей;
   - инструкция: «вот твой прошлый JSON; обнови распределение артикулов по `article`/`article_low_confidence`/`irrelevant` на основании новых данных; все остальные поля сохрани без изменений; ответ — только валидный JSON».
3. `Runner.run_streamed` с той же сессией. SDK сам подтянет полную историю.
4. Парсим, валидируем, перезаписываем `task_runs.result_json`.

#### Фаза 1, Turn 3 — kit_contents-проверка (если is_kit)

Запускается, только если в текущем `result_json` поле `is_kit = true`.

1. Backend делает Exa-вызов «найди состав набора по подтверждённым артикулам». `phase='kit_contents'`.
2. Новое user-сообщение: свежий Exa-ответ + текущий JSON + инструкция «обнови `kit_contents`, остальные поля не трогай».
3. Запуск, парсинг, перезапись `result_json`.

Минимум turn'ов в фазе 1 — 1 (только main). Максимум — 3 (main + low_confidence + kit_contents).

### Фаза 2 — агентский pipeline с тулами

Фаза 2 запускается **всегда** после фазы 1, независимо от того, сколько turn'ов было в фазе 1.

1. Backend **пересоздаёт агента** — с теми же системным промптом и сессией, но теперь с зарегистрированными тулами:
   - `web_search_exa(query: str, num_results: int = 10) -> str`
   - `web_fetch_exa(urls: list[str], max_characters: int = 3000) -> str`
   Реализованы как обычные Python-функции, декорированные `@function_tool`. Внутри функции — обращение к тому же кэширующему Exa-клиенту, с `phase='agent_extra'`.
2. **Лимит**: общий счётчик агентских Exa-вызовов в этом run'е. Считается через `SELECT COUNT(*) FROM exa_cache_usage WHERE run_id=$1 AND phase='agent_extra'`. Когда счётчик достигает 10 — функция-тул возвращает модели сообщение «лимит исчерпан, переходи к финализации» вместо нового вызова. **Один tool_call = один шаг счётчика**, независимо от того, сколько URL в `urls[]` у `web_fetch_exa`.
3. **Жёсткий лимит на сторону Agents SDK**: `Runner.run(..., max_turns=12)` — на случай, если модель залипает в reasoning-цикле без полезных вызовов.
4. Формируется финальное user-сообщение: «вот твой текущий JSON; у тебя есть инструменты `web_search_exa` и `web_fetch_exa`; если есть пустые или сомнительные поля — попробуй их закрыть; лимит на использование Exa — 10 вызовов; когда удовлетворён — отвечай ТОЛЬКО валидным JSON по схеме».
5. `Runner.run_streamed` с той же сессией. Все события стрима пишутся в `agent_stream_events(turn_idx=N+1)`.
6. Завершение run'а — когда модель отвечает без вызова тулов (естественное завершение Agents SDK).
7. Парсим финальный JSON, валидируем, перезаписываем `task_runs.result_json`.

Если в фазе 2 модель упёрлась в `max_turns` без финального текстового сообщения — `failed_validation`.

Если в фазе 2 модель находит новые `low_confidence`-артикулы или новый `is_kit=true`, для которых не было автоматического Exa — **игнорируем**. Фаза 2 финальная, backend не запускает дополнительные автоматические Exa-проходы. Найденное модель помещает в финальный JSON как есть, куратор позже разберёт.

### После фазы 2 — финализация

1. Финальный JSON уже лежит в `task_runs.result_json`.
2. Backend проверяет правила перевода в `needs_human_review`:
   - `is_kit=true` и `kit_contents` пустой → `kit_without_contents`;
   - `product_type=null` → `product_type_unknown`.
3. Backend детерминированно парсит финальный JSON в draft-таблицы (см. раздел «Парсинг JSON → draft-таблицы»).
4. Run помечается `done` или `needs_human_review`.

### Что хранится по итогам run'а

Всё в БД:
- `task_runs.result_json` — финальный JSON.
- `agent_history(session_id='research_run_<id>', item JSONB, ...)` — полная переписка с моделью.
- `agent_stream_events(run_id, turn_idx, seq, event JSONB, ...)` — высокоуровневые streaming-события (только `RunItemStreamEvent` и `AgentUpdatedStreamEvent`, без raw token-deltas).
- `exa_cache.response` — raw Exa-ответы.
- `exa_cache_usage(run_id, cache_id, phase, hit, ...)` — какой run какой Exa использовал.
- `plugin_payloads(run_id, plugin_name, payload)` — Smart-плагин и другие будущие источники.
- `draft_parts`, `draft_part_articles`, `draft_kit_components`, `draft_part_of_kits` — нормализованные данные.

Никаких файлов нигде на диске.

### Что агент НЕ делает

- Не пишет файлы (диска нет).
- Не вызывает MCP-тулы в фазе 1 (их не существует для него).
- Не делает Exa-вызовы в фазе 1 (вообще не имеет такой возможности).
- Не передаёт результат через специальный tool (`write_result` удалён). Финальный результат каждого turn'а — обычный текстовый assistant-message с чистым JSON.

## Структура итогового JSON

Финальный JSON, который research-агент пишет в assistant-message:

- `task_part_number` — входной артикул;
- `name` — название;
- `brand_oem` — массив строк из Smart-брендов (например `["MERCRUISER"]` или `["MERCRUISER", "VOLVO"]`);
- `product_type` — одно из трех Smart-значений, обязательно. Если определить не удалось — `null` + run помечается `needs_human_review`;
- `description` — может быть `null`;
- `weight` — `{kg, source_url, evidence}` или `null`;
- `models` — `{text, source_urls[], evidence}` или `null` (применяемость);
- `is_kit` — boolean (что агент думает);
- `kit_contents` — объект компонентов, ключ — артикул компонента или `unknown_N`;
- `part_of_kits` — массив объектов, если артикул входит в другие наборы;
- `numbers.article`, `numbers.article_low_confidence`, `numbers.irrelevant` — массивы с `source_url` и `evidence` для каждого элемента.

Все правила нормализации Mercury/Quicksilver, нормализации SKU, OEM-only, kit-логики, формата weight в кг — без изменений, описаны в `research_rules.md`.

Pydantic-модель JSON находится в коде; она реализует ровно те же проверки, что были в старой `validation.ts`.

## Валидация артикула на входе

Артикул проверяется на входе в очередь. Регулярка: `^[A-Z0-9\-]+$`, дефис допустим. Кириллица, пробелы, любые другие символы — жесткая ошибка, артикул в очередь не принимается.

При записи в Smart дополнительно работает Smart-валидация `^[A-Z0-9\-]{4,20}$` (длина 4–20). Если найденный кросс-номер короче или длиннее — он не пишется в `smart.parts.articles`, но остается в draft-таблицах и в evidence. Сам входной артикул задачи должен проходить эту валидацию, иначе нет смысла его пытаться записать.

## Парсинг JSON → draft-таблицы

Парсинг детерминированный, делается backend'ом, не агентом. Это важно, чтобы запись в draft не зависела от случайного SQL, который мог бы сгенерировать агент.

Парсинг включает минимальную нормализацию (trim, upper-case артикулов по тем же правилам, что на входе). Никаких «умных» решений — все спорное остается в draft как есть, curator разберется потом.

Draft-таблицы — нормальные реляционные таблицы, не один большой JSONB. Это нужно, чтобы curator мог писать SQL вида «дай все артикулы из всех runs с brand=MERCRUISER и weight=null».

Структура draft-слоя:

- `draft_parts` — основные поля: name, description, brand_oem (массив), product_type, is_kit, weight_kg, weight_source_url, weight_evidence, models_text, models_source_urls (массив), models_evidence, needs_review_reason;
- `draft_part_articles` — все артикулы с разбивкой `confidence ∈ {confirmed, low_confidence, irrelevant}` + `source_url`, `evidence`, и при необходимости `why_low_confidence` / `why_irrelevant`;
- `draft_kit_components` — компоненты набора;
- `draft_part_of_kits` — наборы, в которые входит исследуемый артикул.

При повторном run'е старые draft-записи остаются — они привязаны к `run_id`, и curator смотрит latest. Ничего не удаляется, это messy-база, и история — это фича.

## Smart-плагин и плагины-источники

Система поддерживает плагины-источники. Это внутренний Python-интерфейс. Версии у плагина нет — это просто удобные модули на будущее.

Каждый плагин:

- имеет имя;
- получает на вход артикул задачи;
- возвращает структурированный payload, который сохраняется в `parts_research.plugin_payloads(run_id, plugin_name, payload jsonb)`;
- может подмешивать выжимку в системный промпт research-агента (если плагин «контекстный»).

Smart-плагин — первый и базовый. Логика такая:

- ищет в Smart точное совпадение по нормализованному артикулу + все `part_components`, где артикул является parent или child;
- если ничего не нашлось — плагин ничего не подмешивает;
- если нашлось — собирает выжимку из полей (name, articles, brands, product_type, model, weight_kg, is_draft, список компонентов с article+name+quantity) и кладет в промпт с явной пометкой «это подсказка из Smart, может быть устаревшей, проверь через Exa».

Похожие записи (same brand, same product_type) не подмешиваются — это шум. Только точное совпадение.

Плагиновые данные — подсказка, не истина. Агент может использовать их как направление проверки, но не должен слепо им верить.

## Curator/write-agent

Curator — это судья и редактор перед записью в Smart. Он один на всю систему. Запускается только когда пользователь явно пишет ему в чат сообщение типа «обработай очередь».

Curator не подписан на новые draft автоматически. Он реагирует на сообщения пользователя в чате.

### Технологический стек

Curator — это **Agent** из OpenAI Agents SDK с моделью `cursor-gpt55(high)` и единственным MCP-сервером `parts_research_curator_mcp`.

Тулы куратора подключаются через `MCPServerStreamableHttp` с указанием URL нашего MCP-сервера и заголовков `X-Curator-Session-Id`. Все вызовы тулов проходят через этот сервер и логируются в `agent_sql_log` (для `execute_sql`) и в `curator_messages.tool_call` (для всех).

История чата хранится в `agent_history(session_id='curator_<id>')` через ту же `PostgresSession`-реализацию, что у research-агента.

На этапе 3 общение с куратором — CLI REPL (`python -m parts_research.cli.curator`). На этапе 4 — веб-чат через Next.js + Vercel AI SDK v6 как UI-транспорт; Next.js API route форвардит сообщения в Agents SDK куратора и стримит обратно события `RunItemStreamEvent`, замапленные в формат `useChat`.

### Когда запускается

- Пользователь открывает чат с куратором (CLI REPL или вкладка UI).
- Перед каждым ответом куратора backend добавляет в начало user-сообщения актуальный snapshot очереди: сколько задач в `done` без публикации, краткий список последних N (`task_id`, артикул, `run_id`, что в draft).
- Пользователь пишет «обработай N» или «обработай все».
- Curator идёт читать draft, evidence, Smart через FDW и публикует.

Snapshot пересобирается каждый раз — поэтому очередь, которую видит curator, всегда актуальная.

### Тулы куратора

- `execute_sql({sql})` — сырой SQL по `parts_research` + FDW. Параметризация не вводится — агент сам пишет SQL, в т.ч. многострочные с `BEGIN/COMMIT`. SQL injection не опасен: доступ к куратору только у нас, права БД-юзера ограничены тремя базами. Каждый вызов логируется в `agent_sql_log`. Параллельно-вызываемый.
- `save_to_smart({parts: [...]})` — batch-tool для записи. Каждый part — отдельный SAVEPOINT с per-row try/catch. Подробная спецификация — `save_to_smart.md`. Параллельно-вызываемый (но не одновременно с одним и тем же `smart_id`).
- `mark_needs_review({run_id, reason})` — пометить run как `needs_human_review`. Параллельно-вызываемый.
- `web_search_exa({query, num_results})` / `web_fetch_exa({urls, max_characters})` — прямой Exa через тот же кэширующий клиент, что и у research-агента. Если куратору нужно уточнение, он сам делает поиск.

Отдельный `read_draft` не делаем — всё доступно через `execute_sql`.

Параллельные tool calls — нативная фича Agents SDK и `cursor-gpt55(high)`: модель сама решает, какие вызовы можно сделать в одном turn'е без ожидания результата от предыдущих.

### Сессии куратора

- `curator_sessions(id, started_at, ended_at)` — одна строка на сессию;
- `curator_messages(id, session_id, role, content, tool_call jsonb, created_at)` — визуальная история чата (отдельно от `agent_history`, для UI и аналитики);
- `agent_history(session_id='curator_<id>', item jsonb, ...)` — техническая история input-items для Agents SDK Session;
- `agent_sql_log(id, session_id, sql_text, rows_affected, error, ...)` — лог `execute_sql`.

Новая сессия = новая строка в `curator_sessions` + новая `PostgresSession`. Между сессиями контекст не наследуется. История прошлых сессий доступна как лог.

### Правила записи в Smart

Curator работает по таким правилам (они прописываются в его system-prompt):

- Все новые Smart-записи создаются с `is_draft = true`.
- Связи компонентов создаются с `is_unverified = true`.
- Человек позже снимает флаги вручную в БД.
- Авто-финализацию пока не делаем.
- `can_be_sold_separately` в `part_components` не заполняем — оставляем дефолт (`false`).

Маппинг:

- draft `name` → `smart.parts.name`;
- draft `articles` (только `confirmed`, проходящие Smart-валидацию длины) → `smart.parts.articles`;
- draft `brand_oem` (массив) → `smart.part_brands`. Перед записью curator проверяет через `brand_mapping.brand_aliases`, что значение действительно соответствует одному из Smart-брендов;
- draft `description` → `smart.parts.description`, если описание реально полезное;
- draft `models_text` → `smart.parts.model`;
- draft `product_type` → `smart.parts.product_type` (обязательное);
- draft `weight_kg` → `smart.parts.weight_kg`;
- draft `kit_contents` → `smart.parts` (новые draft-записи для компонентов) + `smart.part_components` (связи).

Если значение неизвестно и Smart-поле допускает null — пишем null.

### Поведение по `is_draft = false`

Основная защита — на уровне сабмита: задача с артикулом, для которого в Smart есть запись с `is_draft = false`, в очередь даже не ставится. Поэтому до куратора такие случаи в норме не доходят.

Если куратор по какой-то причине всё-таки увидел в Smart запись с `is_draft = false` для своей задачи — действие одно: пометить run как `needs_human_review` с reason `smart_finalized_during_research`, в Smart ничего не писать.

Если Smart-запись существует и `is_draft = true` — куратор может дополнять её. Не «перетирать ради перетирания», а заполнять null-поля и добавлять связи компонентов, которых ещё нет.

### Публикации

Связь между run'ами и записями в Smart хранится в таблице `publications(id, run_id, curator_session_id, smart_id, published_at)`. По одному run'у может быть несколько публикаций (parent + draft-компоненты + связи). Это позволяет всегда ответить «откуда эта запись в Smart взялась».

## Наборы и компоненты

Набор — обычная запись `parts`, у которой есть строки в `part_components` как у parent. Компонент — обычная запись `parts`, входящая в набор через `part_components.child_id`.

Система должна уметь работать с:

- обычными одиночными запчастями,
- наборами,
- компонентами наборов,
- запчастями, которые входят в несколько наборов,
- наборами с полным составом,
- наборами с частично известным составом,
- компонентами без найденного артикула.

### Компоненты с артикулами

Если у компонента найден надежный артикул, он записывается в Smart как отдельная draft-запись `parts` + связь `part_components` с `is_unverified = true`.

### Компоненты без артикулов

Источники часто пишут, что в набор входят seals, O-rings, gaskets, washers и пр., но не дают отдельные артикулы. Если такое отбрасывать, состав набора будет неполным.

Поэтому для компонентов без артикула:

- пишем `name`,
- `articles` остается пустым массивом (Smart-схема это позволяет для `is_draft = true`),
- `is_draft = true`,
- связь `is_unverified = true`,
- `description` не заставляем заполнять.

Когда человек позже найдет артикул, он вручную поправит запись в Smart.

### Kit без состава

Это особый случай. Если research-агент сказал `is_kit = true`, но фактически состав найти не удалось (`kit_contents` пустой), задача помечается статусом `needs_human_review`. В Smart никаких kit-записей и компонентов не пишется, потому что набор без состава — это бесполезная запись.

Сам факт `is_kit = true` мы сохраняем в draft, чтобы потом было видно, что агент это утверждал. Но в Smart фактический «набор» создается только когда есть хотя бы один компонент в `kit_contents`.

## Brand mapping в работе

`brand_mapping.brand_aliases` используется в двух местах:

1. **Research-агент** получает таблицу алиасов в своем system-prompt в виде markdown — чтобы возвращать уже нормализованные Smart-бренды и при необходимости массив (VOLVO + MERCRUISER).
2. **Curator** ходит за маппингом через FDW обычным SQL и сам проверяет, что бренд легитимен, перед записью в Smart.

Маппинг считается частью «знания о брендах», а не плагина. Из него всегда получается ОДИН из Smart-брендов. Если нужного алиаса нет — добавляем руками в таблицу.

## Product type

`product_type` — обязательное поле в Smart. Допустимые значения подгружаются из `smart.product_types` и передаются research-агенту в промпте. Агент возвращает один из трех типов.

Маппинг бренд → product_type делать не пытаемся (Honda бывает и автомобильная, и марине). Тип определяет агент по контексту источников. Если определить не удалось — `product_type = null` + run помечается `needs_human_review`.

## UI

Frontend на Next.js (этап 4). Backend для UI — Next.js route handlers, которые ходят в Python-процессы по HTTP. Worker и curator MCP-сервер остаются Python-процессами.

Чат с куратором в UI использует **Vercel AI SDK v6** (`useChat`, streaming events, typed tool/data parts) на стороне клиента. На стороне Next.js API route мы НЕ зовём `streamText` напрямую: вместо этого route форвардит входящее сообщение пользователя в нашу серверную обвязку над Python-сессией куратора и стримит обратно события (текст, mcp_tool_call с in_progress/completed, agent_message). Vercel AI SDK здесь работает как удобный транспортный слой и React-хуки UI, а собственно «мозг» куратора живёт в Python Agents SDK с моделью `cursor-gpt55(high)`.

Чата с research-агентом нет. С ним не переписываемся напрямую, его визуализация не нужна. Прогресс research-runs показывается в UI как карточки задач со стримингом статусов (через polling в начале, при необходимости перейдем на SSE).

UI должен показывать:

- боковую панель добавления задач;
- очередь задач с количеством по статусам (queued, running, done, needs_human_review, failed_*);
- карточки прогресса по артикулам;
- модалку/detail-panel результата с draft-данными, evidence, Smart publication status;
- ошибку внутри карточки;
- чат с куратором (отдельная панель/страница);
- внутри чата куратора — стриминг tool calls (видно, что он выполняет SQL, какие save_to_smart делает);
- источники и evidence;
- неполные компоненты и draft/unverified-флаги.

Визуал должен быть аккуратным и помогать быстро понять: что найдено, что спорное, что записано в Smart, где нужно человеческое внимание, где ошибка.

## Хранение состояния

Всё в БД. Файловой системой система не пользуется в принципе.

Сохраняется:

- финальный JSON каждого run'а — в `task_runs.result_json`;
- история input-items для Agents SDK Session (research и curator) — в `agent_history`;
- высокоуровневые streaming-события (`RunItemStreamEvent`, `AgentUpdatedStreamEvent`) — в `agent_stream_events`. Token-deltas (raw response events) НЕ сохраняются;
- raw Exa-ответы — в `exa_cache.response`;
- использование кэша с фазой — в `exa_cache_usage`;
- сессии куратора, его сообщения, его SQL-лог — в `curator_sessions`, `curator_messages`, `agent_sql_log`;
- публикации — в `publications`;
- плагиновые payloads — в `plugin_payloads`;
- draft-данные — в `draft_*`-таблицах.

## Deploy

Деплой идет по тому же шаблону, что описан в `DEPLOY_TEMPLATE.md`: GHA + Docker Build Cloud + ghcr.io + SSH на `194.164.245.107`. Все три Postgres-контейнера (`parts_research`, `smart_test`, `brands_mapping`) уже подняты на сервере, висят в Docker-сети `db_default`. Новый Postgres поднимать не надо.

Поднимаем только наши процессы:

- `parts_research_worker` — Python-процесс с пулом параллельных research-runs (Agents SDK + Function tools, без MCP).
- `parts_research_curator_mcp` — Python-процесс с MCP-сервером для куратора (FastMCP, Streamable HTTP).
- На этапе 4: `parts_research_app` — Next.js приложение (API + UI), которое ходит в Python-процессы.

Оба Python-процесса собираются из одного `Dockerfile` (multi-stage с `uv pip install --system`), различаются только командой запуска в `docker-compose.yml`. Образ — `python:3.13-slim` базовый.

Никаких volume'ов не используется (диск не нужен). Никакого `~/.codex/auth.json`. Никакого mihomo HTTP-proxy.

## Бэкап и destructive операции

SQL tool куратора технически не ограничен по типу запросов (SELECT/INSERT/UPDATE/DELETE и т.д.). UI-подтверждение для каждого destructive SQL не делаем. Предполагается, что базы регулярно бэкапятся (стандартный pg_dump).

В инструкциях куратору прописано: сначала изучать данные, не делать бессмысленных destructive-действий, объяснять себе изменения, ориентироваться на правила записи в Smart, логировать выполненный SQL.

## Backend-first порядок реализации

Сначала делаем backend и проверяем логику без фронта. План MVP — в `IMPLEMENTATION_PLAN.md`.

## Зафиксированные решения

- `parts_research` — основная рабочая база ресерча. Сбрасываем и пересоздаём с новой DDL под Python-реализацию.
- `smart_test` — база чистых Smart-результатов (используем именно эту, не prod Smart).
- `brands_mapping` — отдельная reusable база на `194.164.245.107:5411`.
- Smart и brand_mapping доступны из `parts_research` через `postgres_fdw`.
- **Никаких файлов на диске**: финальный JSON в `task_runs.result_json`, stream — в `agent_stream_events`, история — в `agent_history`.
- Raw Exa-ответы хранятся только в БД-кэше.
- Парсинг итогового JSON в draft-таблицы делает backend, детерминированно.
- Draft-данные привязаны к task и run, нормальные реляционные таблицы.
- Exa-cache exact-match по hash от tool+args (без run_id), `exa_cache_usage` дополнен полем `phase`.
- **Research-агент работает в две фазы.** Фаза 1 (1–3 turn'а): backend сам делает Exa, передаёт raw результат модели, у модели нет тулов, она пишет JSON в текст. Фаза 2 (всегда после фазы 1): модель получает `web_search_exa` / `web_fetch_exa` как Python-function-тулы, может дозаполнять пробелы, лимит 10 агентских Exa, `max_turns=12`.
- **Модель отдаёт финальный JSON как обычное assistant-сообщение в тексте** (не через специальный tool). `write_result` как MCP-tool удалён.
- **В каждое user-сообщение фазы 1 backend подмешивает текущий JSON модели явно** — как страховка от потери полей.
- Brand mapping передается агенту через текст промпта, куратору — через FDW SQL.
- Список Smart-брендов и product_type загружается из Smart и подмешивается в промпт research-агента.
- Research-агент может вернуть массив брендов (VOLVO + MERCRUISER и т.п.).
- product_type обязателен; если не определен — `needs_human_review`.
- Smart-плагин подмешивает в промпт research-агента выжимку по точному совпадению артикула.
- **Технологически:** Python 3.13 + uv + Agents SDK (openai-agents) + asyncpg + pydantic + mcp (только для куратора).
- **LLM-эндпоинт** OpenAI-совместимый: `http://194.164.245.107:8317/v1`, ключ `local-gpt55`, модель `cursor-gpt55(high)` для обоих агентов. Параметры через env.
- **Только один curator** на всю систему. Реализован на Agents SDK с MCP-сервером `parts_research_curator_mcp`. У куратора **нет фаз** — он сразу видит тулы.
- На этапе 3 общение с куратором — CLI REPL. На этапе 4 — веб-чат через Vercel AI SDK v6.
- Curator запускается только когда пользователь пишет ему в чат «обработай».
- Snapshot очереди подмешивается в начало каждого user-сообщения куратору.
- У куратора четыре tool'а через MCP: `execute_sql`, `save_to_smart` (batch с per-row try/catch), `web_search_exa`/`web_fetch_exa`, `mark_needs_review`. Все параллельно-вызываемые.
- **PostgresSession** — наша реализация Session-протокола Agents SDK поверх таблицы `agent_history`. Используется обоими агентами.
- **Никакого MCP для research-агента.** Его тулы — Python-функции с `@function_tool`, выполняются в процессе worker'а.
- Защита от записи в финализированные `is_draft = false` — на submit-уровне.
- `can_be_sold_separately` в `smart.part_components` не заполняем; остаётся дефолт `false`.
- Curator не делегирует точечный допоиск другим агентам — сам делает Exa.
- Smart-записи по умолчанию `is_draft = true`, связи компонентов `is_unverified = true`.
- Человек вручную финализирует записи в БД.
- Компоненты без артикулов разрешены как draft (с `name`, без `description`).
- Kit без состава → `needs_human_review`.
- Aftermarket-only / weight в нераспознанных единицах → `failed_no_data`.
- Невалидный JSON от модели → `failed_validation` (без авто-ретраев; доработка через промпт).
- Зависшие в `running` >30 минут → `failed_crashed`.
- Перезапуск задачи создает новый run; история runs сохраняется.
- Очередь FIFO без приоритетов, параллелизм 30 через `asyncio.Semaphore`.
- SQL tool куратора без ограничений по типу запросов; права БД-юзера ограничены тремя базами.
- Schema создается одним SQL-файлом, миграции — отдельными SQL через терминал. Никаких ORM-миграторов.
- Деплой: GHA + DBC + ghcr.io + SSH; новые контейнеры (`parts_research_worker`, `parts_research_curator_mcp`, `parts_research_app`) висят в `db_default` Docker-сети рядом с уже живыми postgres-контейнерами.

## Что сейчас не делаем

- Fuzzy Exa-cache.
- Передача старых research results каждому новому research-агенту.
- Сложный workflow engine.
- Автофинализацию Smart-записей.
- Обязательное подтверждение каждого destructive SQL через UI.
- Хранение raw evidence и Exa-ответов внутри Smart.
- Отношение к плагиновым данным как к гарантированной истине.
- Требование полностью найти все компоненты набора перед draft-публикацией (компоненты без артикулов разрешены).
- Отдельный Smart-флаг `has_missing_article`.
- Отдельную Smart-колонку `is_kit`.
- Прод-Smart-миграцию (работаем только в smart_test).
- Версионирование `research_rules.md` и промптов.
- Heartbeat для worker'а (живем с просто 30-минутным таймаутом).
- Авто-ретраи на `failed_validation` и `failed_no_data`.
- Rate-limit retry для Exa и LLM API (добавим позже при необходимости).
- Поиск похожих записей в Smart-плагине (только exact match по нормализованному артикулу).
- Приоритеты в очереди.
- MCP-обёртку для research-агента (его тулы — Python-функции, MCP избыточен).
- Дополнительные «фазы 1.5» в случае, если в фазе 2 модель находит новые low_confidence / новый kit.
- Authentication, мультиюзер, авторизацию в UI.
- Хранение raw token-deltas в `agent_stream_events` (только высокоуровневые события).
- Детерминированный non-destructive merge JSON между turn'ами фазы 1 (вместо этого — явная инструкция модели + явное включение предыдущего JSON в каждое user-сообщение).
- Auto-обработку ситуации, когда фаза 2 закончилась с пустыми обязательными полями (всё решает куратор и/или человек).

## Итоговая логика

Система хранит messy research в `parts_research`, где он полезен, и держит `smart_test` настолько чистым, насколько это возможно для draft-каталога.

`parts_research` отвечает за все промежуточное: поиск, источники, доказательства, финальные JSON артефакты, draft, Exa-cache, плагины, SQL-логи, сессии куратора, действия агентов, потоки событий.

`smart_test` отвечает за каталожную форму: parts, brands, components, draft/final state, unverified relations.

`brands_mapping` отвечает за нормализацию OEM-названий брендов в Smart-формат и переиспользуется в других проектах.

Research-агент работает в две фазы: backend ведёт за руку через детерминированный Exa-pipeline, потом модель сама дозаполняет с тулами. Куратор — обычный agentic loop с MCP.

Детерминированные части остаются в backend: кэширование Exa, парсинг JSON в draft, публикация draft в Smart, логирование SQL.

Так система остается простой, проверяемой и расширяемой без лишней архитектуры. Никаких файлов на диске, никаких внешних брокеров очередей, никакого внешнего LLM-провайдера кроме одного OpenAI-совместимого эндпоинта.
