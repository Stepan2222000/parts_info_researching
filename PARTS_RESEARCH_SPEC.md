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

- `parts` — запчасти и наборы. Важные поля: `id` (генерится сам как `smart_XXXXXXXX`), `name`, `articles TEXT[]` (валидируется regex `^[A-Z0-9\-]{4,20}$`, без дублей внутри массива, для опубликованных записей обязателен минимум один артикул), `description`, `vehicle_classes TEXT[]` (слаги классов техники; колонки `product_type` в parts с миграции 015 НЕТ — тип вычисляется во VIEW из классов), `model`, `weight_kg NUMERIC(8,3)`, `is_draft`.
- `brands(name PK)` — справочник Smart-брендов в UPPER_SNAKE_CASE. Точный список не хардкодим в коде — грузим из `smart.brands` через FDW при старте каждого run'а и подмешиваем в system-prompt research-агента.
- `part_brands(part_id, brand)` — M:N связка. Для `is_draft = false` минимум один бренд обязателен.
- `part_articles(article PK, part_id)` — глобально-уникальный реестр обычных артикулов, синхронизируется триггером.
- `part_components(parent_id, child_id, quantity, can_be_sold_separately, is_unverified)` — состав наборов, циклы запрещены триггером.
- `vehicle_classes(slug PK, title_ru, product_type, season_months, position)` — классы техники: boat, jetski, quad, snowmobile, motorcycle, auto. `product_types(name PK)` остаётся словарём грубых типов, на который классы ссылаются.
- `parts_with_components` — view с разворотом компонентов и вычисленным `is_kit` через `EXISTS`.

В Smart мы не храним raw Exa-ответы, длинные evidence-тексты, логи агента и историю research-JSON. Smart хранит итоговую каталожную форму.

Отдельной колонки `is_kit` в Smart нет и не нужно. Набор определяется по наличию строк в `part_components`, где запчасть является `parent_id`.

### `brands_mapping`

`brands_mapping` — отдельная база на хосте `2.27.20.221:5411`, db `brands_mapping`. Это reusable справочник, который будет использоваться и в других проектах.

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

Worker пуллит свободные задачи через `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` внутри транзакции, помечает `running` и тут же атомарно отпускает блокировку. Параллелизм — 30 одновременно выполняющихся research-runs (управляется через `asyncio.Semaphore`). Дополнительные задачи лежат в `queued` и ждут. Воркеров может быть несколько — все они разбирают одну общую очередь; задачи обрабатываются постепенно по мере освобождения воркеров.

### Постановка задач и возврат результата (`cli.research`)

Единственная точка входа в очередь сейчас — `cli.research ARTICLE [ARTICLE ...]`. Это **submit-and-wait** поверх той же общей очереди воркеров:

1. Для каждого артикула: нормализация + regex-валидация + FDW submit-guard (`is_draft=false` → отказ по этому артикулу). Создаётся `task` + `task_run` в статусе `queued`. Невалидный/отказанный артикул не валит остальные — он попадает в итоговый массив со своим `status` (`invalid` / `refused`) и `error`, без `run_id`.
2. Команда проверяет, есть ли живой воркер (см. «Liveness воркеров»). Если живого воркера нет — задачи остаются в очереди (обработаются, когда воркер поднимется), а команда **сразу** возвращает фидбек по каждому артикулу (`{status:"queued", worker_alive:false, run_id}`) и завершается, не зависая.
3. Если воркер жив — команда поллит `task_runs.status` по своим `run_id` (~1 c) до терминального статуса (`done` / `needs_human_review` / `failed_*`), без жёсткого таймаута. Если живые воркеры исчезли посреди ожидания — это видно по `pg_locks`, и команда возвращает текущий `queued`/`running`-статус с пометкой `worker_alive:false`.
4. На выходе — **JSON-массив** (по объекту на артикул) в stdout: `{article, task_id, run_id, status, error, needs_review_reason, result_json}`. Логи/прогресс — в stderr. Никаких файлов на диске; результат именно возвращается вызывающему (в т.ч. агенту).

`cli.research` ничего не публикует в Smart — `done`-run без строки в `publications` и есть «очередь для куратора». Параллельные `cli.research` и фоновые воркеры делят одну очередь.

### Liveness воркеров

Чтобы `cli.research` мог сразу дать фидбек при отсутствии воркера, каждый воркер на старте берёт **shared advisory-lock** на фиксированный ключ (`pg_advisory_lock_shared`) и держит его весь свой lifetime на отдельном соединении. `cli.research` читает `pg_locks` (`locktype='advisory'`, `mode='ShareLock'`, наш ключ): ненулевое число держателей → есть живые воркеры. Несколько воркеров берут ключ в shared-режиме — они совместимы и все видны в `pg_locks`. Лок освобождается автоматически при завершении/падении процесса воркера — отдельной очистки не нужно. Само lock-соединение воркер пингует keepalive'ом (~30 c) и при разрыве переподключается и берёт lock заново — чтобы idle-таймаут сети/NAT не уронил liveness на простаивающем воркере. Это **не** полноценный heartbeat-мониторинг (периодических статусов нет); восстановление зависших `running`-задач по-прежнему делает 30-минутный stale-таймаут.

### Защита на этапе постановки в очередь

Перед `INSERT INTO tasks` постановка в очередь (`cli.research`) обязательно делает проверку через FDW:

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
- `failed_crashed` — worker упал во время выполнения. Восстанавливается так: при старте worker помечает все `running`-задачи старше 30 минут как `failed_crashed`. Полноценного heartbeat-мониторинга нет — только liveness через advisory-lock (см. «Liveness воркеров»);
- `needs_human_review` — задача дошла до финального JSON, но это пограничный случай, нужен ручной взгляд (kit без состава, не определены vehicle_classes, и т.п.).

`needs_human_review` нужен для случаев, когда система фактически собрала данные, но не имеет права автоматически их публиковать. Эти задачи не failure — они просто ждут человека.

Aftermarket-only находки, weight в нераспознанных единицах, провал валидации — это НЕ `needs_human_review`, это `failed_no_data` или `failed_validation`.

Перезапуск задачи (`re-run`) создает новый run с новым `run_id`. Старые runs остаются в БД и видны как история. Draft-таблицы всегда привязаны к конкретному run'у. Curator при работе смотрит только последний `done`-run по задаче, но может через SQL заглянуть и в старые, если нужно.

## Стек технологий

End-to-end Python.

- **Python 3.13+**, async-first. Глобальный рантайм ставится через `mise` (`mise use -g python@latest`); версионный файл в репо не создаём.
- Пакеты ставятся в глобальный site-packages mise-python через `python -m pip install …`. Зависимости перечислены в `pyproject.toml` — служит только декларацией состава, lock-файлы не используем.
- **`openai-agents`** — фреймворк агентов (Agents SDK от OpenAI). Используется и для research-агента, и для куратора. См. ссылки в разделе «Документация».
- **`openai`** — официальный клиент. Используется внутри Agents SDK для общения с эндпоинтом через `AsyncOpenAI + OpenAIChatCompletionsModel`.
- **`asyncpg`** — async-драйвер Postgres. Все обращения к БД через пул соединений (default `min_size=10, max_size=60`).
- **`pydantic` v2** — типизация и парсинг JSON модели. Field-валидаторы строго на то, чего нет в JSON-схеме (например `min_length`).
- **`exa-py`** — Python-клиент Exa REST API. Используется и в backend-Exa фазы 1, и внутри агентских тулов фазы 2.
- **`python-dotenv`** — чтение `.env`-файлов.
- **PostgreSQL 18** — все три БД (`parts_research`, `smart_test`, `brands_mapping`) уже подняты на сервере как отдельные контейнеры; новые БД не поднимаем. `postgres_fdw` для связи.

Никаких Node.js, npm, TypeScript, Codex SDK, `uv`, `~/.codex/auth.json`, mihomo HTTP-proxy. Авторизация LLM — обычный Bearer-токен в заголовке.

## LLM-эндпоинт

Используется OpenAI-совместимый Chat Completions эндпоинт:

- Base URL: `http://2.27.20.221:8317/v1`
- API key: `cliproxy-e2602729e9e53a01885c91350fb852f735ce` (Bearer-токен)
- Модель research-агента: `cursor-gpt55(high)` (gpt-5.5 с high reasoning)
- Модель куратора: `cursor-gpt55(high)`

Параметры берутся из env-переменных (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL_RESEARCH`, `LLM_MODEL_CURATOR`), чтобы менять без правки кода.

Подключение к эндпоинту делается через стандартный путь Agents SDK: `AsyncOpenAI(base_url, api_key)` → `OpenAIChatCompletionsModel(model, openai_client)` → `Agent(..., model=...)`.

Tracing Agents SDK отключаем (`set_tracing_disabled(True)`), потому что эндпоинт не openai.com.

## Архитектура процессов

Долгоживущих процессов два (плюс уже существующие Postgres-контейнеры на сервере). MCP-серверов в системе нет: оба агента (research и curator) живут каждый в своём Python-процессе и пользуются тулами как `@function_tool` напрямую.

### Process 1 — `parts_research_worker`

Основной долгоживущий процесс. Содержит:

- Pipeline research-агента целиком (фаза 1 + фаза 2).
- Пул соединений `asyncpg` к `parts_research`.
- Цикл «взять следующую `queued`-задачу через `FOR UPDATE SKIP LOCKED` → выполнить pipeline → пометить готовой/ошибочной».
- `asyncio.Semaphore(30)` для ограничения параллелизма.
- При старте: берёт shared advisory-lock и держит его весь lifetime как liveness-сигнал для `cli.research` (см. «Liveness воркеров»); затем чистит зависшие `running`-задачи старше 30 минут (`failed_crashed`).

Тулы research-агента в фазе 2 (`web_search_exa`, `web_fetch_exa`) реализованы как `@function_tool` Agents SDK прямо в коде worker'а. Кэш Exa и счётчик agent_extra вызовов — внутри того же процесса (детали ниже в разделах «Кэш Exa-вызовов» и «Лимит Exa в фазе 2»).

### Process 2 — `parts_research_curator`

Долгоживущий процесс с курсор-агентом. Содержит:

- Курсор-агент (Agents SDK, модель `cursor-gpt55(high)`) с тулами как `@function_tool` в коде того же процесса:
  - `execute_sql(sql)` — сырой SQL по `parts_research` + FDW. Логируется в `agent_sql_log`.
  - `save_to_smart(parts)` — batch-публикация в Smart, см. `save_to_smart.md`.
  - `mark_needs_review(run_id, reason)` — пометить run как `needs_human_review`.
  - `web_search_exa(query, num_results)` / `web_fetch_exa(urls, max_characters)` — прямые вызовы Exa **без кэша** (у курсора Exa-запросы редкие и контекстные; кэш живёт только в research-процессе).
- Пул `asyncpg` к `parts_research`.
- Один из транспортов общения с пользователем:
  - на этапе 3 — CLI REPL (stdin/stdout);
  - на этапе 4 — тонкий HTTP-API (FastAPI, эндпоинт `POST /curator/message` со streaming SSE), который форвардит сообщения в `Runner.run_streamed` курсор-агента. Это **наш** HTTP-эндпоинт, не MCP-сервер.

### Короткоживущие команды

- `python -m parts_research.cli.research ARTICLE [ARTICLE ...]` — кладёт артикулы в **общую очередь воркеров** и ждёт их обработки, затем печатает результат(ы) как JSON-массив в stdout (агент-facing; см. «Постановка задач и возврат результата»). Если живого воркера нет — задачи всё равно ставятся в очередь, а команда сразу возвращает фидбек (`worker_alive:false`), не зависая.
- `python -m parts_research.cli.curator` — открывает CLI REPL курсора (этап 3 до появления UI).

`cli.submit` (bulk fire-and-forget без ожидания результата) пока не делаем — `cli.research` покрывает и постановку, и ожидание. Добавим, если понадобится массовая загрузка без чтения результата.

## Research-агент

Research-агент работает в **две фазы**. Фаза 1 — детерминированная, ведёт backend. Фаза 2 — агентская, ведёт модель.

### Общий setup (до первого вызова модели)

Backend параллельно подгружает контекст:

1. Список Smart-брендов (для валидации `brand_oem`).
2. Справочник классов техники `smart.vehicle_classes` (slug, title_ru, position).
3. `brand_mapping.brand_aliases` через FDW.
4. Smart-plugin payload (точное совпадение по артикулу + связанные `part_components` parents/children).

Также читается `research_rules.md` — жёсткие правила OEM/Mercury/kit/нормализации.

Из всего этого собирается **системный промпт** агента. Он одинаковый для всех turn'ов одного run'а:

- общие правила (`research_rules.md`),
- классы техники (слаги vehicle_classes с названиями),
- допустимые Smart-бренды,
- таблица алиасов брендов (markdown),
- Smart-подсказка (если есть точное совпадение по артикулу),
- описание желаемого JSON-формата и схемы,
- инструкция: «модель отвечает ТОЛЬКО валидным JSON по схеме, без markdown-обёртки, без комментариев, без лишнего текста».

Создаётся **PostgresSession** с `session_id = f"research_run_{run_id}"`. Это реализация Session-протокола Agents SDK поверх таблицы `agent_history` (методы `get_items`, `add_items`, `pop_item`, `clear_session`).

### Слои валидации

Финальный JSON каждого turn'а проходит **три непересекающихся слоя** валидации:

1. **OpenAI strict JSON schema** (`output_type=StructuredResult` → `response_format={"type":"json_schema","strict":true,...}`) — структура, типы, обязательность полей, `additionalProperties=false`. Это серверная гарантия: модель не может вернуть лишнее поле или неверный тип.
2. **Pydantic-валидаторы** на полях, не выразимых в strict-схеме: запрет пустых строк через `AfterValidator` (а **не** `Field(min_length=1)` — иначе `minLength` попал бы в JSON-схему, а часть эндпоинтов strict-mode такой ключ отклоняет), плюс проверка непустоты массива `models.source_urls`.
3. **Доменная пост-валидация** (Python-функция `post_validate`) — правила с runtime-данными: `brand_oem ⊂ allowed_brands`, `vehicle_classes ⊆ allowed_vehicle_classes` (без дублей; кросс-типовый мультикласс легален), `task_part_number == expected`, `task_part_number ∈ numbers.article`, `is_kit=false ⇒ kit_contents пуст`, артикул в `numbers.*` встречается не более одного раза по массивам, `kit_contents[i].article ≠ task_part_number и любому из numbers.article`.

Каждое правило живёт **только в одном слое**. Дубли запрещены.

Любой провал любого слоя → run помечается `failed_validation` (без авто-ретраев).

### Фаза 1 — детерминированный pipeline (1–4 turn'а)

В фазе 1 у модели **нет никаких тулов**. Она получает порцию данных и обязана ответить чистым JSON в текстовом сообщении.

#### Фаза 1, Turn 1 — основной Exa-поиск

1. Backend сам делает Exa-вызов `web_search_exa({query, numResults=10})` с детерминированной формулировкой запроса по артикулу. Вызов проходит через кэширующий слой: hash от tool_name + canonical JSON args → SELECT в `exa_cache`; если попадание — берём оттуда (`exa_cache_usage.hit=true`), иначе зовём реальный Exa, сохраняем (`exa_cache_usage.hit=false`). В обоих случаях `exa_cache_usage.phase = 'main'`.
2. Backend проверяет точное вхождение артикула в raw-ответе через substring-match по нижнему регистру JSON-сериализации **без нормализации** (артикул передаётся как пришёл — с дефисами, в исходном виде после `.strip().upper()`). Никакой OEM-нормализации (срез префиксов Mercury `26-`/`12-`, замена дефисов) здесь нет — это доменная логика, она в `research_rules.md` и применяется моделью, а не защитной проверкой backend'а. Артикулы, для которых Exa нормализует дефисы иначе, могут попадать в `failed_no_data` — известное ограничение. Если вхождения нет — run = `failed_no_data`, pipeline останавливается.
3. Backend создаёт **агента без тулов** и составляет первое user-сообщение: компактный pick из Exa-ответа (`url, title, highlights`) + инструкция «сформируй стартовый JSON по схеме».
4. Запускается `Runner.run_streamed(agent, input=user_msg, session=session)`. Все события стрима пишутся в `agent_stream_events(run_id, turn_idx=1, seq, event)`.
5. По завершении: SDK сам парсит ответ в `StructuredResult` (strict-схема гарантирует структуру). Применяются слои 2 и 3 валидации. Результат записывается в `task_runs.result_json`.

#### Фаза 1, Turn 2 — family-expansion (если есть подтверждённые кроссы)

Запускается, только если turn 1 дал хотя бы один подтверждённый кросс кроме входного артикула (`numbers.article` минус сам входной номер).

1. Backend делает Exa-вызов, засеянный **подтверждёнными кроссами** (не входным номером), с детерминированной формулировкой «перечисли всё семейство преемственности этих OEM-номеров». `phase='family_expansion'`. `substring_check` на входной артикул здесь **не выполняется** — на страницах-родственниках его законно может не быть.
2. Новое user-сообщение: свежий Exa-ответ (только `highlights`) + текущий JSON + инструкция «добавь пропущенных соседей семейства: уверенные → `numbers.article`, спорные → `article_low_confidence`; ранее `irrelevant` можно перенести при явном новом подтверждении; входной и уже подтверждённые не трогай; остальные поля сохрани».
3. Полный текст страниц на этом этапе **не читается** — это остаётся работой агента в фазе 2 (`web_fetch_exa`). Парсим, валидируем (включая `post_validate` — входной и подтверждённые не должны пропасть), перезаписываем `result_json`.

#### Фаза 1, Turn 3 — low_confidence-проверка (если есть)

Запускается, только если в текущем (после family-expansion) `result_json` массив `numbers.article_low_confidence` непуст.

1. Backend делает Exa-вызов с детерминированной формулировкой «проверь эти артикулы как OEM-кроссы для исходного X». `phase='low_confidence'`.
2. Формируется новое user-сообщение. В него явно включается:
   - свежий Exa-ответ (raw JSON);
   - **текущий JSON модели** (целиком, прямо в тексте) — как страховка от потери полей;
   - инструкция: «вот твой прошлый JSON; обнови распределение артикулов по `article`/`article_low_confidence`/`irrelevant` на основании новых данных; все остальные поля сохрани без изменений; ответ — только валидный JSON».
3. `Runner.run_streamed` с той же сессией. SDK сам подтянет полную историю.
4. Парсим, валидируем, перезаписываем `task_runs.result_json`.

#### Фаза 1, Turn 4 — kit_contents-проверка (если is_kit)

Запускается, только если в текущем `result_json` поле `is_kit = true`.

1. Backend делает Exa-вызов «найди состав набора по подтверждённым артикулам». `phase='kit_contents'`.
2. Новое user-сообщение: свежий Exa-ответ + текущий JSON + инструкция «обнови `kit_contents`, остальные поля не трогай».
3. Запуск, парсинг, перезапись `result_json`.

Минимум turn'ов в фазе 1 — 1 (только main). Максимум — 4 (main + family_expansion + low_confidence + kit_contents).

### Фаза 2 — агентский pipeline с тулами

Фаза 2 запускается **всегда** после фазы 1, независимо от того, сколько turn'ов было в фазе 1.

1. Backend **пересоздаёт агента** — с тем же системным промптом и сессией, но теперь с зарегистрированными тулами:
   - `web_search_exa(query: str, num_results: int = 10) -> str`
   - `web_fetch_exa(urls: list[str], max_characters: int = 3000) -> str`
   Реализованы как обычные Python-функции, декорированные `@function_tool`. Внутри функции — обращение к тому же кэширующему Exa-клиенту, с `phase='agent_extra'`.
2. **Лимит Exa-вызовов в фазе 2**: счётчик в памяти процесса, по умолчанию 10. Живёт в `RunContextWrapper.context` (передаётся через `Runner.run(..., context=...)`) и инкрементируется внутри `@function_tool` функций. Когда счётчик достигает лимита — функция-тул возвращает модели сообщение «лимит исчерпан, финализируй JSON» вместо нового вызова. **Один tool_call = один шаг счётчика**, независимо от того, сколько URL в `urls[]` у `web_fetch_exa`. Счётчик скоупится одним run'ом; после завершения run'а он не нужен. Факт каждого вызова всё равно пишется в `exa_cache_usage` (для постфактум-аналитики), но runtime-лимит проверяется в памяти, без SQL-COUNT на каждый tool-call.
3. **Жёсткий лимит итераций**: `Runner.run(..., max_turns=12)` — на случай, если модель залипает в reasoning-цикле без полезных вызовов. Если упёрлась без финального текстового сообщения — `failed_validation`.
4. **Backend в фазе 2 не делает substring-check** на Exa-результаты от агентских тулов. Свобода поиска принадлежит модели: она может искать имя детали, серию, фитмент, и эти результаты не обязаны содержать сам task_part_number. Защита от ложной релевантности — **инструкция в промпте**: «используй только источники, в которых явно встречается артикул задачи».
5. Формируется финальное user-сообщение фазы 2: текущий JSON + инструкция «у тебя есть тулы web_search_exa и web_fetch_exa, лимит N вызовов, используй только источники с артикулом задачи; если есть пустые/сомнительные поля — попробуй их закрыть; когда удовлетворён — отвечай ТОЛЬКО валидным JSON по схеме».
6. `Runner.run_streamed` с той же сессией. Все события стрима пишутся в `agent_stream_events(turn_idx=N+1)`.
7. Завершение run'а — когда модель отвечает без вызова тулов (естественное завершение Agents SDK). К финальному JSON применяются все три слоя валидации.

Если в фазе 2 модель находит новые `low_confidence`-артикулы или меняет `is_kit` — она сама же дозаполняет их через свои тулы. Backend дополнительные автоматические Exa-проходы НЕ запускает. Финальный JSON — последнее слово модели; куратор разберёт, что осталось спорным.

### Retry на network errors

На обоих агентах выставляется `ModelSettings(retry=ModelRetrySettings(max_retries=2, policy=retry_policies.network_error()))` — штатный механизм Agents SDK для повтора при transient transport errors. Это **не** auto-retry на бизнес-ошибки модели или провал валидации.

Важно (проверено эмпирически): встроенный SDK-retry **не повторяет обрывы, случившиеся уже посреди стрима** — это запрещено из соображений replay-safety (см. доку «Runner-managed retries»). А именно такие обрывы — `httpx.ReadError` и «голый» `openai.APIError: stream error … INTERNAL_ERROR` (HTTP/2 RST_STREAM) — составляют основную долю сбоев нашего эндпоинта (~7% turn-вызовов). Поэтому backend дополнительно оборачивает каждый turn в **собственный retry-loop** (до 2 повторов), повторяющий turn целиком при транспортной ошибке. Так как история turn'ов передаётся явно (через `to_input_list()` / PostgresSession), повтор replay-safe. Бизнес-ошибки (`APIStatusError` 4xx/5xx, `MaxTurnsExceeded`, провал валидации) в этот retry не попадают — отличаем их по типу (транспортный «голый» `APIError`/`APIConnectionError`/`httpx.*` против `APIStatusError`).

### После фазы 2 — финализация

1. Финальный JSON уже лежит в `task_runs.result_json`.
2. Backend проверяет правила перевода в `needs_human_review`:
   - `is_kit=true` и `kit_contents` пустой → `kit_without_contents`;
   - `vehicle_classes=[]` → `vehicle_classes_unknown`.
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
- Не имеет тулов в фазе 1 — ему не передаётся `tools=[...]`. Exa-вызовы в фазе 1 делает только backend.
- Не передаёт результат через специальный tool (никакого `write_result` нет). Финальный результат каждого turn'а — обычный текстовый assistant-message с чистым JSON, парсится в `StructuredResult` по strict-схеме.

## Структура итогового JSON

Финальный JSON, который research-агент пишет в assistant-message:

- `task_part_number` — входной артикул.
- `name` — название.
- `brand_oem` — массив строк из Smart-брендов (например `["MERCRUISER"]` или `["MERCRUISER", "VOLVO"]`).
- `vehicle_classes` — массив слагов классов техники (может быть несколько, в т.ч. разных типов: Rotax → jetski+snowmobile); `[]`, если определить не удалось (run → `needs_human_review`).
- `description` — может быть `null`.
- `weight` — `{kg, source_url, evidence}` или `null`.
- `models` — `{text, source_urls[], evidence}` или `null`.
- `is_kit` — boolean.
- `kit_contents` — **массив объектов** компонентов набора. У каждого: `article: string | null` (`null`, если артикул не найден), `name`, `quantity: int | null`, `description: string | null`, `source_url`, `evidence`. Если `is_kit=false` — массив пустой.
- `part_of_kits` — массив объектов, если артикул входит в другие наборы.
- `numbers.article`, `numbers.article_low_confidence`, `numbers.irrelevant` — массивы с `source_url`, `evidence` (плюс `why_low_confidence` / `why_irrelevant` соответственно).

Все правила нормализации Mercury/Quicksilver, нормализации SKU, OEM-only, kit-логики, формата weight в кг — описаны в `research_rules.md` и применяются моделью. В защитной проверке backend'а (substring-check) они не применяются.

Pydantic-модель `StructuredResult` живёт в коде; формальный контракт совпадает с этим разделом. Strict JSON schema, которую SDK строит из этой модели, передаётся OpenAI-эндпоинту через `response_format` (см. слой 1 в разделе «Слои валидации»).

## Валидация артикула на входе

Артикул проверяется на входе в очередь. Регулярка: `^[A-Z0-9\-]+$`, дефис допустим. Кириллица, пробелы, любые другие символы — жесткая ошибка, артикул в очередь не принимается.

При записи в Smart дополнительно работает Smart-валидация `^[A-Z0-9\-]{4,20}$` (длина 4–20). Если найденный кросс-номер короче или длиннее — он не пишется в `smart.parts.articles`, но остается в draft-таблицах и в evidence. Сам входной артикул задачи должен проходить эту валидацию, иначе нет смысла его пытаться записать.

## Парсинг JSON → draft-таблицы

Парсинг детерминированный, делается backend'ом, не агентом. Это важно, чтобы запись в draft не зависела от случайного SQL, который мог бы сгенерировать агент.

Парсинг включает минимальную нормализацию (trim, upper-case артикулов по тем же правилам, что на входе). Никаких «умных» решений — все спорное остается в draft как есть, curator разберется потом.

Draft-таблицы — нормальные реляционные таблицы, не один большой JSONB. Это нужно, чтобы curator мог писать SQL вида «дай все артикулы из всех runs с brand=MERCRUISER и weight=null».

Структура draft-слоя:

- `draft_parts` — основные поля: name, description, brand_oem (массив), vehicle_classes (массив слагов от агента), product_type (деривация из классов — тип класса с min position, только для морды), is_kit, weight_kg, weight_source_url, weight_evidence, models_text, models_source_urls (массив), models_evidence, needs_review_reason.
- `draft_part_articles` — все артикулы с разбивкой `confidence ∈ {confirmed, low_confidence, irrelevant}` + `source_url`, `evidence`, и при необходимости `why_low_confidence` / `why_irrelevant`.
- `draft_kit_components(draft_part_id, component_key, article, name, quantity, description, source_url, evidence)` — компоненты набора. Колонка `component_key` существует только в draft-таблице (в JSON от модели её нет): backend генерирует её **детерминированно** при парсинге — `article`, если он не `null`; иначе `unknown_1`, `unknown_2`, … в порядке появления компонента в массиве `kit_contents`.
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
- если нашлось — собирает выжимку из полей (name, articles, brands, vehicle_classes, model, weight_kg, is_draft, список компонентов с article+name+quantity) и кладет в промпт с явной пометкой «это подсказка из Smart, может быть устаревшей, проверь через Exa».

Похожие записи (same brand, same vehicle class) не подмешиваются — это шум. Только точное совпадение.

Плагиновые данные — подсказка, не истина. Агент может использовать их как направление проверки, но не должен слепо им верить.

## Curator/write-agent

Curator — это судья и редактор перед записью в Smart. Он один на всю систему. Запускается только когда пользователь явно пишет ему в чат сообщение типа «обработай очередь».

Curator не подписан на новые draft автоматически. Он реагирует на сообщения пользователя в чате.

### Технологический стек

Curator — это `Agent` из OpenAI Agents SDK с моделью `cursor-gpt55(high)`. Все его тулы реализованы как `@function_tool` в коде того же процесса (`parts_research_curator`). MCP-сервера у курсора нет: внешних потребителей у него нет, HTTP-обвязка ему не нужна. Логирование вызовов идёт прямо в тулах через `asyncpg`-пул (в `agent_sql_log` для `execute_sql`, в `curator_messages.tool_call` для всех остальных).

История чата хранится в `agent_history(session_id='curator_<id>')` через ту же `PostgresSession`-реализацию, что у research-агента.

На этапе 3 общение с куратором — CLI REPL (`python -m parts_research.cli.curator`) в том же процессе, что и агент. На этапе 4 — веб-чат через Next.js + Vercel AI SDK v6 как UI-транспорт; Next.js API route форвардит сообщения в наш FastAPI-эндпоинт `POST /curator/message` (тонкая обвязка над `Runner.run_streamed`), а тот стримит обратно события `RunItemStreamEvent`, замапленные в формат `useChat`. FastAPI здесь — наш собственный HTTP-API, не MCP.

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
- `web_search_exa({query, num_results})` / `web_fetch_exa({urls, max_characters})` — прямые вызовы Exa **без кэша**. У курсора Exa-запросы редкие и контекстные; кэш живёт только в research-процессе.

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
- draft `vehicle_classes` → `smart.parts.vehicle_classes` (обязателен непустой при INSERT; при UPDATE merge-only);
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

`vehicle_classes` — эталонная классификация в Smart. Справочник подгружается из `smart.vehicle_classes` и передаётся research-агенту в промпте. Агент возвращает массив слагов (можно несколько, кросс-тип легален).

Жёсткий маппинг бренд-корпорация → класс делать не пытаемся (Honda бывает и автомобильная, и марине; BRP делает гидроциклы, снегоходы и квадры). Классы агент определяет по моделям применимости; маркеры линеек даны в research_rules.md. Если определить не удалось — `vehicle_classes = []` + run помечается `needs_human_review`.

## UI

Frontend на Next.js (этап 4). Backend для UI — Next.js route handlers, которые ходят в наш Python-процесс курсора по HTTP (FastAPI-эндпоинт `POST /curator/message` со streaming SSE). Worker и `parts_research_curator` остаются Python-процессами; никаких MCP-серверов нет.

Чат с куратором в UI использует **Vercel AI SDK v6** (`useChat`, streaming events, typed tool/data parts) на стороне клиента. На стороне Next.js API route мы НЕ зовём `streamText` напрямую: route форвардит сообщение пользователя в `/curator/message` Python-процесса, который запускает `Runner.run_streamed` и стримит обратно события (текст, tool_call с in_progress/completed, agent_message), замапленные в формат `useChat`. Vercel AI SDK — UI-транспорт; «мозг» куратора живёт в Python Agents SDK с моделью `cursor-gpt55(high)`.

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

Деплой идет по тому же шаблону, что описан в `DEPLOY_TEMPLATE.md`: GHA + Docker Build Cloud + ghcr.io + SSH на `2.27.20.221`. Все три Postgres-контейнера (`parts_research`, `smart_test`, `brands_mapping`) уже подняты на сервере, висят в Docker-сети `db_default`. Новый Postgres поднимать не надо.

Поднимаем только наши процессы:

- `parts_research_worker` — Python-процесс с пулом параллельных research-runs (Agents SDK + `@function_tool`, без MCP).
- `parts_research_curator` — Python-процесс с курсор-агентом и его тулами (`@function_tool` в том же процессе). На этапе 3 в нём поднимается CLI REPL; на этапе 4 — FastAPI-эндпоинт `POST /curator/message` для UI.
- На этапе 4: `parts_research_app` — Next.js приложение (API + UI), которое ходит в `parts_research_curator` по HTTP и в `parts_research_worker` (или прямо в БД через FDW-views) для статуса задач.
- `parts_research_public_api` (`cli.public_api`) — отдельный публичный HTTP-процесс **только для внешних систем**: `POST /research` (submit-and-wait), `GET /research/{run_id}`, `GET /health`. Намеренно без эндпоинтов куратора и UI-выборок (куратор умеет SQL и запись в Smart — наружу не выставляется). Единственный сервис с проброшенным наружу портом; без аутентификации. Контракт для внешних описан в `EXTERNAL_API.md`.

Все Python-процессы собираются из одного `Dockerfile` (multi-stage с `pip install --no-cache-dir`), различаются только командой запуска в `docker-compose.yml`. Образ — `python:3.13-slim` базовый.

Никаких volume'ов не используется (диск не нужен). Никакого `~/.codex/auth.json`. Никакого mihomo HTTP-proxy. Никаких MCP-серверов.

## Бэкап и destructive операции

SQL tool куратора технически не ограничен по типу запросов (SELECT/INSERT/UPDATE/DELETE и т.д.). UI-подтверждение для каждого destructive SQL не делаем. Предполагается, что базы регулярно бэкапятся (стандартный pg_dump).

В инструкциях куратору прописано: сначала изучать данные, не делать бессмысленных destructive-действий, объяснять себе изменения, ориентироваться на правила записи в Smart, логировать выполненный SQL.

## Backend-first порядок реализации

Сначала делаем backend и проверяем логику без фронта. План MVP — в `IMPLEMENTATION_PLAN.md`.

## Зафиксированные решения

- `parts_research` — основная рабочая база ресерча. Сбрасываем и пересоздаём с новой DDL под Python-реализацию.
- `smart_test` — база чистых Smart-результатов (используем именно эту, не prod Smart).
- `brands_mapping` — отдельная reusable база на `2.27.20.221:5411`.
- Smart и brand_mapping доступны из `parts_research` через `postgres_fdw`.
- **Никаких файлов на диске**: финальный JSON в `task_runs.result_json`, stream — в `agent_stream_events`, история — в `agent_history`.
- Raw Exa-ответы хранятся только в БД-кэше.
- Парсинг итогового JSON в draft-таблицы делает backend, детерминированно.
- Draft-данные привязаны к task и run, нормальные реляционные таблицы.
- Exa-cache exact-match по hash от tool+args (без run_id), `exa_cache_usage` дополнен полем `phase`.
- **Research-агент работает в две фазы.** Фаза 1 (1–4 turn'а): backend сам делает Exa, передаёт raw результат модели, у модели нет тулов, она пишет JSON в текст. Turn'ы: main → family_expansion (если у turn 1 есть подтверждённые кроссы — добор соседей по семейству, засев самими кроссами) → low_confidence → kit_contents. Фаза 2 (всегда после фазы 1): модель получает `web_search_exa` / `web_fetch_exa` как Python-function-тулы, может дозаполнять пробелы, лимит 10 агентских Exa, `max_turns=12`.
- **Модель отдаёт финальный JSON как обычное assistant-сообщение в тексте** (не через специальный tool). `write_result` как MCP-tool удалён.
- **В каждое user-сообщение фазы 1 backend подмешивает текущий JSON модели явно** — как страховка от потери полей.
- Brand mapping передается агенту через текст промпта, куратору — через FDW SQL.
- Список Smart-брендов и справочник классов техники загружается из Smart и подмешивается в промпт research-агента.
- Research-агент может вернуть массив брендов (VOLVO + MERCRUISER и т.п.).
- vehicle_classes обязательны; если не определены — `needs_human_review`.
- Smart-плагин подмешивает в промпт research-агента выжимку по точному совпадению артикула.
- **Технологически:** Python 3.13+ (mise) + Agents SDK (openai-agents) + openai + exa-py + asyncpg + pydantic v2 + python-dotenv. Без `uv`, без venv в репо, без MCP — пакеты в глобальный site-packages mise-python.
- **Strict JSON schema** через `output_type=StructuredResult` — OpenAI на стороне эндпоинта гарантирует структуру/типы/required/`additionalProperties=false`. Валидаторы Pydantic покрывают то, чего нет в strict-схеме (запрет пустых строк через `AfterValidator`, непустота `models.source_urls`); доменные правила — в `post_validate`. Слои не пересекаются.
- **Retry на сетевые ошибки** на обоих агентах: `ModelSettings(retry=ModelRetrySettings(max_retries=2, policy=retry_policies.network_error()))` + собственный turn-level retry-loop backend'а на обрывы посреди стрима (которые встроенный SDK-retry не ловит по replay-safety). Только transient transport errors, не бизнес-ошибки.
- **`kit_contents` — массив объектов** без поля `key`. `article: string | null`. `component_key` для `draft_kit_components` генерирует backend при парсинге.
- **Substring-check** в backend-Exa фазы 1 — без OEM-нормализации (дефисы/префиксы передаются как пришли). Артикулы, по которым Exa нормализует дефисы иначе, могут попадать в `failed_no_data` — известное поведение.
- **В фазе 2 backend substring-check не делает** — релевантность контролирует модель через инструкцию в промпте «используй только источники, где явно встречается артикул задачи».
- **LLM-эндпоинт** OpenAI-совместимый: `http://2.27.20.221:8317/v1`, ключ `cliproxy-e2602729e9e53a01885c91350fb852f735ce`, модель `cursor-gpt55(high)` для обоих агентов. Параметры через env.
- **Только один curator** на всю систему. Реализован на Agents SDK; тулы курсора — `@function_tool` в коде того же процесса. У курсора **нет фаз** — он сразу видит тулы.
- На этапе 3 общение с куратором — CLI REPL в `parts_research_curator`. На этапе 4 — веб-чат: Next.js + Vercel AI SDK v6 → наш FastAPI-эндпоинт `POST /curator/message` → `Runner.run_streamed` курсор-агента. MCP-сервера ни у курсора, ни у research-агента нет.
- Curator запускается только когда пользователь пишет ему в чат «обработай».
- Snapshot очереди подмешивается в начало каждого user-сообщения куратору.
- У курсора пять тулов: `execute_sql`, `save_to_smart` (batch с per-row try/catch), `mark_needs_review`, `web_search_exa`, `web_fetch_exa`. Все параллельно-вызываемые. Exa у курсора **без кэша** (кэш только в research-процессе).
- **PostgresSession** — наша реализация Session-протокола Agents SDK поверх таблицы `agent_history`. Используется обоими агентами.
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
- Очередь FIFO без приоритетов, параллелизм 30 через `asyncio.Semaphore`. Воркеров может быть несколько на одну общую очередь.
- **Постановка в очередь — через `cli.research` (submit-and-wait):** кладёт один или несколько артикулов в общую очередь воркеров, ждёт обработки и возвращает результат как JSON-массив в stdout (агент-facing). `cli.submit` (bulk fire-and-forget) отложен.
- **Liveness воркеров — shared advisory-lock**, читаемый `cli.research` через `pg_locks`. Нет живого воркера → задачи всё равно `queued`, команда сразу отдаёт фидбек (`worker_alive:false`), без зависания.
- SQL tool куратора без ограничений по типу запросов; права БД-юзера ограничены тремя базами.
- Schema создается одним SQL-файлом, миграции — отдельными SQL через терминал. Никаких ORM-миграторов.
- Деплой: GHA + DBC + ghcr.io + SSH; новые контейнеры (`parts_research_worker`, `parts_research_curator`, на этапе 4 ещё `parts_research_app`) висят в `db_default` Docker-сети рядом с уже живыми postgres-контейнерами.

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
- Полноценный heartbeat-мониторинг воркеров (периодические статусы, авто-алерты). Есть только минимальный liveness-сигнал — shared advisory-lock, который `cli.research` читает через `pg_locks`, чтобы сразу дать фидбек при отсутствии живого воркера. Восстановление зависших — по 30-минутному stale-таймауту (`failed_crashed`).
- Авто-ретраи на `failed_validation` и `failed_no_data`.
- Rate-limit retry для Exa и LLM API (добавим позже при необходимости).
- Поиск похожих записей в Smart-плагине (только exact match по нормализованному артикулу).
- Приоритеты в очереди.
- MCP-сервера ни для кого. Все тулы обоих агентов — `@function_tool` в коде агентского процесса; внешних потребителей тулов нет, кэш Exa требуется только в research-процессе.
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

Research-агент работает в две фазы: backend ведёт за руку через детерминированный Exa-pipeline, потом модель сама дозаполняет через `@function_tool`. Курсор — обычный agentic loop, тоже с `@function_tool` в одном процессе с агентом. MCP-серверов в системе нет.

Детерминированные части остаются в backend: кэширование Exa, парсинг JSON в draft, публикация draft в Smart, логирование SQL.

Так система остается простой, проверяемой и расширяемой без лишней архитектуры. Никаких файлов на диске, никаких внешних брокеров очередей, никакого внешнего LLM-провайдера кроме одного OpenAI-совместимого эндпоинта.

## Документация и ссылки

Основные источники для всех технологий, упомянутых выше.

### Agents SDK и LLM

- OpenAI Agents SDK для Python: <https://openai.github.io/openai-agents-python/>
  - Конфигурация и кастомные клиенты: <https://openai.github.io/openai-agents-python/config/>
  - Модели и `OpenAIChatCompletionsModel`: <https://openai.github.io/openai-agents-python/models/>
  - Сессии (Session-протокол): <https://openai.github.io/openai-agents-python/sessions/>
  - Streaming (`Runner.run_streamed`): <https://openai.github.io/openai-agents-python/streaming/>
  - Function tools (`@function_tool`): <https://openai.github.io/openai-agents-python/tools/#function-tools>
  - Retry policies (`retry_policies.network_error`): <https://openai.github.io/openai-agents-python/ref/run_internal/model_retry/>
- OpenAI Python SDK (используется внутри Agents SDK): <https://github.com/openai/openai-python>
- Структурированный вывод (Structured Outputs, `response_format=json_schema`): <https://platform.openai.com/docs/guides/structured-outputs>

### Exa

- Документация Exa: <https://docs.exa.ai/>
- Python SDK `exa-py`: <https://docs.exa.ai/reference/python-sdk>, репозиторий <https://github.com/exa-labs/exa-py>
- Search API guide: <https://docs.exa.ai/reference/search>

### Pydantic

- Pydantic v2 docs: <https://docs.pydantic.dev/latest/>
- Field constraints (`min_length`, и т.п.): <https://docs.pydantic.dev/latest/concepts/fields/>
- Field validators (`@field_validator`): <https://docs.pydantic.dev/latest/concepts/validators/>

### PostgreSQL и драйвер

- PostgreSQL 18 docs: <https://www.postgresql.org/docs/current/>
- `postgres_fdw`: <https://www.postgresql.org/docs/current/postgres-fdw.html>
- `SELECT … FOR UPDATE SKIP LOCKED` (основа очереди): <https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE>
- asyncpg: <https://magicstack.github.io/asyncpg/current/>

### Инфраструктура

- `mise` (управление рантаймами): <https://mise.jdx.dev/>
- `python-dotenv`: <https://github.com/theskumar/python-dotenv>
- Docker Build Cloud (используется в CI): <https://docs.docker.com/build/cloud/>
- GitHub Actions: <https://docs.github.com/en/actions>

### Фронтенд (этап 4)

- Next.js: <https://nextjs.org/docs>
- Vercel AI SDK v6 (`useChat`, streaming): <https://sdk.vercel.ai/docs>

