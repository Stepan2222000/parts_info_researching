# План реализации

Этот документ описывает, в каком порядке мы строим систему ресерча запчастей. Спецификация всей системы — в `PARTS_RESEARCH_SPEC.md`. Правила research-агента — в `research_rules.md`, правила куратора — в `curator_rules.md`, спецификация `save_to_smart` — в `save_to_smart.md`.

План разбит на четыре этапа. Первые три — backend на Python. Четвёртый — frontend на Next.js. Каждый этап заканчивается рабочей системой, которую можно прогнать через терминал и убедиться, что она делает то, что должна. Никакого overengineering и никаких бессмысленных фолбеков.

## Что мы НЕ переносим, а пишем заново

Старая TypeScript-реализация (`src/`, `research_part.ts`, `package.json`, `Dockerfile`, `docker-compose.yml`, `storage/`, `codex_results/`, `exa_results/`, `prompts/`, `readable_results/`, `node_modules/`) **полностью удаляется**. Это сознательное решение: TS-кодбейз был построен вокруг Codex SDK, sandbox-режима и файлового хранилища — всё это в новой архитектуре не используется. Логику переписываем с нуля под Python + Agents SDK + только-БД-хранилище.

Сохраняем:
- `migrations/001_init.sql` (с правками под новую схему — описано в этапе 0);
- `research_rules.md` — правила работы агента, переименовано из `codex_rules.md`;
- `curator_rules.md`, `save_to_smart.md` — обновлены под новый стек;
- `PARTS_RESEARCH_SPEC.md`, `IMPLEMENTATION_PLAN.md` — этот документ и спека;
- `.env`, `.github/` — рабочие.

База `parts_research` дропается и пересоздаётся (там нет ничего ценного, что нельзя было бы воссоздать).

## Общие правила работы по этапам

Перед началом каждого этапа делается короткий поиск в интернете по темам, с которыми работаем впервые, чтобы не наступить на свежие грабли. Уже изученные темы заново не перепроверяем.

Перед тем как писать код любой нетривиальной части, прогоняем её через терминал в простой форме — убеждаемся, что выбранный подход в принципе работает. Только после такой проверки переходим к реализации.

В конце каждого этапа прогоняем полную систему через терминал на нескольких реальных артикулах. Если что-то падает — чиним до перехода к следующему этапу.

---

## Прототип (до Этапа 0) — `research_part.py`

Перед началом Этапа 0 в репо есть отладочный скрипт `research_part.py` для одного артикула. Он реализует фазу 1 и фазу 2 research-агента на тестовом stub-окружении (без БД parts_research, без worker'а, без файлового хранилища) — единственная цель: убедиться, что LLM-эндпоинт, Agents SDK, strict JSON schema, Pydantic, Exa-py работают как описано в `PARTS_RESEARCH_SPEC.md`. Полная спецификация скрипта — в `research_part.md`. Прототип не часть production-системы, но именно на нём отлаживается контракт `StructuredResult` и поведение модели на разных артикулах.

---

## Этап 0 — Фундамент проекта и DDL

**Цель этапа.** Подготовить пустой Python-проект, обновлённую DDL под новую схему `parts_research`, и проверить, что все базовые подключения работают: к LLM-эндпоинту, к Postgres, к Exa API (`exa-py`), к FDW.

**Что делаем.**

Удаляем старый TS-кодбейз и связанные с ним директории. Создаём чистый Python-проект:

- `pyproject.toml` с минимальным набором зависимостей: `openai-agents`, `openai`, `exa-py`, `asyncpg`, `pydantic`, `python-dotenv`. Lock-файл не используем — пакеты ставятся в глобальный site-packages mise-python.
- Python ставится глобально через `mise use -g python@latest` (см. глобальный CLAUDE.md).
- Базовая структура `src/parts_research/{config,db,research,curator,plugins,queue,cli}/`.
- `Dockerfile` (Python 3.13+ slim, multi-stage с `pip install --no-cache-dir`).
- `docker-compose.yml` с двумя сервисами: `worker` и `curator`. Образ один и тот же, команды разные.

Обновляем `migrations/001_init.sql` под новую схему:

- Удаляем поля `task_runs.codex_thread_id`, `task_runs.storage_dir`.
- Добавляем поле `task_runs.result_json JSONB`.
- Добавляем таблицу `agent_history(id BIGSERIAL, session_id TEXT, item JSONB, created_at TIMESTAMPTZ)` с индексом по `(session_id, id)`.
- Добавляем таблицу `agent_stream_events(id BIGSERIAL, run_id BIGINT, turn_idx INTEGER, seq INTEGER, event JSONB, created_at TIMESTAMPTZ)` с индексом по `(run_id, turn_idx, seq)`.
- Добавляем колонку `phase TEXT` в `exa_cache_usage`.
- Удаляем поля `curator_sessions.codex_thread_id`, `curator_sessions.working_dir`.
- Остальные таблицы (`tasks`, `task_runs`, `draft_*`, `exa_cache`, `plugin_payloads`, `curator_sessions`, `curator_messages`, `agent_sql_log`, `publications`) остаются.
- FDW-блок (smart, brand_mapping) остаётся как есть.

Дропаем существующую БД `parts_research` на сервере и накатываем обновлённую миграцию через psql.

Пишем `src/parts_research/config.py` с чтением всех env-переменных: `PARTS_RESEARCH_DATABASE_URL`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL_RESEARCH`, `LLM_MODEL_CURATOR`, `EXA_API_KEY`, `WORKER_CONCURRENCY`, `WORKER_STALE_MINUTES`.

Пишем `src/parts_research/db/pool.py` — asyncpg пул с `min_size=10, max_size=60`.

Проверяем через короткий terminal-скрипт:

- что `AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY).chat.completions.create(model=LLM_MODEL_RESEARCH, messages=[{"role":"user","content":"ping"}])` возвращает ответ;
- что `asyncpg.connect(PARTS_RESEARCH_DATABASE_URL)` подключается и видит все таблицы новой схемы;
- что FDW работает (`SELECT * FROM smart.parts LIMIT 1`, `SELECT * FROM brand_mapping.brand_aliases LIMIT 1`).

**Что не делаем (откладывается).**

- Логику research-агента (этап 1).
- Worker-пул (этап 2).
- Curator (этап 3).
- UI (этап 4).

**Готовность.** Зависимости установлены (`python -m pip install -e .` или поштучно). Команда `python -m parts_research.cli.ping` (отладочный скрипт, тестирующий все три подключения) — отрабатывает и печатает «OK».

---

## Этап 1 — Research-pipeline для одного артикула

**Цель этапа.** Реализовать полный pipeline research-агента (фаза 1 + фаза 2) для одного артикула. Никакой очереди, никакого worker'а — простая команда `python -m parts_research.cli.research ARTICLE`, которая создаёт `task`, `task_run` и гоняет один артикул через все turn'ы.

**Что делаем.**

Пишем `src/parts_research/research/exa_client.py` — кэширующий Exa-клиент. Это асинхронная функция `cached_exa_call(tool_name: str, args: dict, run_id: int | None, phase: str | None) -> dict`:

- Считает hash от `tool_name + canonical_json(args)`.
- `SELECT response FROM exa_cache WHERE request_hash = $1`.
- На попадание — `INSERT INTO exa_cache_usage (cache_id, run_id, hit, phase)` (только если `run_id` не None) и возвращает кэш.
- На промах — зовёт реальный Exa API через `exa-py` (`exa.search(...)`), сохраняет в `exa_cache`, делает `exa_cache_usage` с `hit=false`, возвращает ответ.
- Никаких файлов не пишет.

Пишем `src/parts_research/research/prompts.py` — функции `build_main_user_message`, `build_low_confidence_user_message`, `build_kit_contents_user_message`, `build_phase2_user_message`. Системный промпт (`build_system_prompt`) подгружает `research_rules.md`, список Smart-брендов, product_types, brand_aliases, Smart-plugin payload.

Пишем `src/parts_research/research/schema.py` — Pydantic-модель финального JSON (`StructuredResult`), с теми же проверками что в старой `validation.ts`: непустые строки, массив брендов из allowed, product_type из allowed, наличие `task_part_number` в `numbers.article`, отсутствие собственного артикула в `kit_contents`, и т.п.

Пишем `src/parts_research/db/session.py` — реализация `PostgresSession` (наследник `SessionABC` из `agents.memory.session`):

- `__init__(self, session_id: str, pool: asyncpg.Pool)`.
- `get_items(limit=None)` — `SELECT item FROM agent_history WHERE session_id=$1 ORDER BY id`.
- `add_items(items)` — bulk-insert.
- `pop_item()` — `DELETE ... RETURNING item` с подзапросом по последнему id.
- `clear_session()` — `DELETE WHERE session_id=$1`.

Пишем `src/parts_research/research/agent_factory.py` — функция `make_research_agent(system_prompt, tools=None)` создаёт `Agent` с `OpenAIChatCompletionsModel`, `tools=[...]` (для фазы 2) или без тулов (для фазы 1).

Пишем function-тулы `web_search_exa`, `web_fetch_exa` (с `@function_tool`), которые внутри:

- Считают через `SELECT COUNT(*) FROM exa_cache_usage WHERE run_id=$1 AND phase='agent_extra'`. Если ≥10 — возвращают модели текст «лимит исчерпан».
- Иначе вызывают `cached_exa_call(...)` с `phase='agent_extra'`.

`run_id` пробрасывается в тулы через `Agent`-context (`@function_tool` поддерживает `ctx: RunContextWrapper`).

Пишем `src/parts_research/research/streaming.py` — обёртка `run_streamed_and_persist(agent, input, session, run_id, turn_idx)`:

- Запускает `Runner.run_streamed(agent, input=input, session=session, max_turns=12)`.
- Перебирает `stream_events()`, фильтрует только `RunItemStreamEvent` и `AgentUpdatedStreamEvent`, сериализует и пишет в `agent_stream_events`.
- Возвращает финальный assistant text для парсинга.

Пишем `src/parts_research/research/run.py` — главная функция `execute_run(run_id: int, article: str)`:

1. `ensure_running(run_id)`.
2. Параллельно подгружает контекст (4 запроса в БД).
3. Создаёт `PostgresSession(f"research_run_{run_id}", pool)`.
4. **Фаза 1, Turn 1**: основной Exa, проверка вхождения, создаёт агента без тулов, `run_streamed_and_persist(turn_idx=1)`, парсит, валидирует, пишет в `task_runs.result_json`.
5. **Фаза 1, Turn 2** (если есть low_confidence): второй Exa, новое user-сообщение с явным включением предыдущего JSON, `run_streamed_and_persist(turn_idx=2)`, парсит, валидирует, перезаписывает result_json.
6. **Фаза 1, Turn 3** (если is_kit): третий Exa, аналогично, `turn_idx=3`.
7. **Фаза 2**: создаёт нового агента с function-тулами, `run_streamed_and_persist(turn_idx=N+1, max_turns=12)`, парсит, валидирует, перезаписывает result_json.
8. Определяет `needs_human_review_reason`.
9. Детерминированно парсит финальный JSON в `draft_*`-таблицы.
10. `finish_run(run_id, 'done' or 'needs_human_review')`.

На исключениях:
- `NoExactDataError` → `failed_no_data`.
- `ValidationError` → `failed_validation`.
- Любая другая → `failed_crashed`.

Пишем `src/parts_research/cli/research.py` — entry point, который:

- Делает submit-guard (через FDW проверяет `is_draft = false`).
- Создаёт `tasks` row и `task_runs` row в статусе `queued`.
- Зовёт `execute_run(run_id, article)`.
- Печатает результат.

**Что не делаем (откладывается).**

- Worker и очередь (этап 2). На этом этапе всё запускается одной командой.
- Curator (этап 3).
- UI (этап 4).

**Готовность.** Гоняем `python -m parts_research.cli.research 807252T5` (или другой реальный артикул). В терминале видим стрим событий (tool_called, message_output_created). В БД появляются: `tasks`, `task_runs` со статусом `done` (или `needs_human_review`), `task_runs.result_json` заполнен, `agent_history` содержит всю переписку, `agent_stream_events` содержит события всех turn'ов, `exa_cache` пополнен, `exa_cache_usage` имеет строки с разными `phase`, `draft_parts` и связанные таблицы заполнены. Прогоняем 3–5 разных артикулов: одиночная деталь, kit с известным составом, артикул где product_type неочевиден.

> **Примечание (уточнено позже).** На Этапе 1 `cli.research` запускал pipeline **inline** (без очереди и воркера) — это была отладочная веха для контракта `execute_run`. В Этапе 2 `cli.research` переопределяется в **submit-and-wait** поверх общей очереди воркеров (см. ниже); inline-режим как отдельная команда не сохраняется.

---

## Этап 2 — Общая очередь воркеров и agent-facing `cli.research`

**Цель этапа.** Ввести общий пул воркеров с одной очередью и переопределить `cli.research` в **submit-and-wait**: команда кладёт артикулы в очередь, воркеры разбирают её постепенно, команда дожидается результата и возвращает его как JSON вызывающему (в т.ч. агенту). После этого этапа research-часть закончена.

**Что делаем.**

Пишем `src/parts_research/db/tasks.py` — DB-helpers: `create_task`, `create_queued_run`, `pick_next_queued_run` (внутри транзакции с `FOR UPDATE SKIP LOCKED`), `ensure_running`, `finish_run`, `mark_crashed_stale_runs`. Плюс liveness-helpers: `acquire_worker_lock(conn)` (`pg_advisory_lock_shared` на фиксированном ключе) и `count_live_workers(pool)` (через `pg_locks`).

Пишем `src/parts_research/queue/worker.py` — главный цикл воркера:

- При старте: берёт **shared advisory-lock** на фиксированном ключе на отдельном соединении и держит его весь lifetime — liveness-сигнал для `cli.research`. Затем `mark_crashed_stale_runs(timeout_minutes=WORKER_STALE_MINUTES)`.
- Бесконечный async-цикл:
  - Если `len(inflight) >= WORKER_CONCURRENCY` — `await asyncio.wait(inflight, return_when=FIRST_COMPLETED)`.
  - Иначе `next = await pick_next_queued_run()`. Если `None` — `await asyncio.sleep(poll_interval)`, иначе `asyncio.create_task(execute_run(...))`, добавляем в `inflight`.
- На SIGINT/SIGTERM — graceful drain: ждём всех inflight, потом выходим (advisory-lock освободится сам при закрытии соединения).

Несколько воркеров берут один и тот же ключ в shared-режиме — они совместимы и все видны в `pg_locks`; параллелизм каждого — `asyncio.Semaphore(WORKER_CONCURRENCY)`.

Переопределяем `src/parts_research/cli/research.py` в **submit-and-wait** (см. «Постановка задач и возврат результата» в спеке):

- Принимает один или несколько артикулов.
- Для каждого: нормализация + regex + FDW submit-guard; создаёт `task` + `queued` run. Невалидный/отказанный артикул не валит остальные — попадает в итог со своим `status` (`invalid` / `refused`), без `run_id`.
- Проверяет живого воркера через `pg_locks`. Нет воркера → возвращает по каждому артикулу `{status:"queued", worker_alive:false, run_id}` сразу, не зависая.
- Есть воркер → поллит `task_runs.status` (~1 c) до терминального статуса без жёсткого таймаута; если живые воркеры исчезли посреди ожидания — возвращает текущий статус с `worker_alive:false`.
- Печатает **JSON-массив** результатов в stdout, логи — в stderr.

Пишем `src/parts_research/cli/worker.py` — entry point, запускает `worker.run_forever()` с обработкой SIGINT/SIGTERM.

Обновляем `Dockerfile` так, чтобы образ умел запускать worker через `python -m parts_research.cli.worker`. В `docker-compose.yml` добавляем сервис `worker`.

**Что не делаем (откладывается).**

- `cli.submit` (bulk fire-and-forget без ожидания результата) — пока не нужен, `cli.research` покрывает постановку и ожидание. Добавим при необходимости массовой загрузки без чтения результата.
- Полноценный heartbeat-мониторинг воркеров (только advisory-lock liveness).
- Curator (этап 3), деплой на сервер (конец этапа 3), UI (этап 4).

**Готовность.** Запускаем `python -m parts_research.cli.worker` локально (можно несколько процессов). В отдельном терминале — `python -m parts_research.cli.research 807252T5 295100923 76868A04` с разными артикулами: команда ставит их в общую очередь, воркеры параллельно разбирают, команда дожидается и печатает JSON-массив результатов в stdout. Останавливаем все воркеры и снова зовём `cli.research` — команда сразу отдаёт `worker_alive:false` и не виснет. Перезапускаем воркер во время работы — `failed_crashed` отрабатывает на следующем старте.

---

## Этап 3 — Curator (CLI REPL) и деплой

**Цель этапа.** Подключить куратора, который превращает накопившийся draft в записи Smart, и в конце этапа развернуть всё на сервере. После этого этапа backend полностью готов, остаётся только UI.

**Что делаем.**

Пишем тулы курсора как `@function_tool` в `src/parts_research/curator/tools.py`. Все они живут в том же Python-процессе, что и курсор-агент; никакого MCP-сервера нет.

- `execute_sql(ctx, sql: str) -> str` — выполняет SQL на `parts_research` через `asyncpg`-пул. До выполнения — `INSERT INTO agent_sql_log (session_id, sql_text, started_at)`; после — `UPDATE agent_sql_log SET rows_affected=…, error=…, finished_at=now()`. Для SELECT возвращает rows, для остальных — `row_count`. `ctx.context` содержит `curator_session_id`.
- `save_to_smart(ctx, parts: list[dict]) -> list[dict]` — семантика из `save_to_smart.md`: каждый part в своём SAVEPOINT, INSERT vs UPDATE по `smart_id`, проверки `is_draft`/`is_unverified`, patch-merge компонентов, запись в `publications`.
- `mark_needs_review(run_id: int, reason: str) -> str` — UPDATE `task_runs.status = 'needs_human_review'`.
- `web_search_exa(query: str, num_results: int = 10) -> str` — прямой `exa.search(...)`, БЕЗ кэша (у курсора Exa-запросы редкие и контекстные).
- `web_fetch_exa(urls: list[str], max_characters: int = 3000) -> str` — прямой `exa.get_contents(urls, text=True)`, с обрезкой по `max_characters`.

Пишем `src/parts_research/curator/snapshot.py` — `load_snapshot()` и `format_snapshot()`.

Пишем `src/parts_research/curator/agent_factory.py` — `make_curator_agent(session_id: str)`:

- Создаёт `Agent(name="curator", instructions=curator_system_prompt, model=OpenAIChatCompletionsModel(...), tools=[execute_sql, save_to_smart, mark_needs_review, web_search_exa, web_fetch_exa], model_settings=ModelSettings(retry=retry_policies.network_error(max_retries=2)))`.
- Системный промпт собирается из `curator_rules.md` + ссылка на `save_to_smart.md`.

Пишем `src/parts_research/curator/repl.py` — async-REPL:

- При старте: `INSERT INTO curator_sessions (started_at)` → получает `session_id`.
- Создаёт `PostgresSession(f"curator_{session_id}", pool)`.
- Цикл: читает строку из stdin, добавляет `<queue>...</queue>` snapshot в начало, пишет user-message в `curator_messages`, запускает `Runner.run_streamed(agent, input=..., session=session, context=CuratorRunContext(session_id=session_id, pool=pool, exa=exa))`. Перебирает события, рендерит в stdout, дублирует tool calls и assistant messages в `curator_messages`.
- Команды `/exit`, `/new`.

Пишем `src/parts_research/cli/curator.py` — entry point для REPL.

Обновляем `docker-compose.yml`: добавляем сервис `curator` с командой `python -m parts_research.cli.curator`. Образ тот же, что у `worker`.

**Деплой.**

После того как `worker` и `curator` REPL работают локально на нескольких реальных артикулах, поднимаем prod-деплой по шаблону `DEPLOY_TEMPLATE.md`:

- GHA workflow собирает Docker-образ через Docker Build Cloud, пушит в ghcr.io.
- SSH-деплой на `194.164.245.107`: `docker compose pull && docker compose up -d`.
- `parts_research_worker` и `parts_research_curator` поднимаются в `db_default` Docker-сети рядом с Postgres-контейнерами.
- Никаких volume'ов не нужно (диск не используется).
- env-переменные кладутся через `.env` в директорию деплоя.

Прогоняем на сервере: submit пары артикулов → worker их обрабатывает → подключаемся к серверу по SSH → запускаем `docker exec -it parts_research_curator python -m parts_research.cli.curator` → пишем «обработай все» → проверяем `smart.parts`, `smart.part_brands`, `smart.part_components`, `publications`.

**Что не делаем (откладывается).**

- Frontend — этап 4.
- Polling/SSE для прогресса — без UI бессмысленно.
- Новые плагины кроме Smart.
- Авто-финализация.
- DB-триггер `is_draft = false` в Smart.

**Готовность.** На сервере: worker обрабатывает очередь, curator REPL работает, в `publications` появляются строки, в `smart.parts` — draft-записи. Все сценарии (одиночная деталь, kit с полным составом, kit без артикулов у компонентов, kit без состава = `needs_human_review`, артикул уже в Smart как `is_draft=true`, артикул с `is_draft=false` отказывается на submit) — отрабатывают корректно.

---

## Этап 4 — Frontend на Next.js

**Цель этапа.** Дать пользователю удобный интерфейс над всей системой.

**Что делаем.**

Поднимаем `parts_research_app` контейнер с Next.js. Бэкенд приложения = Next.js route handlers; они ходят в Python-процессы по HTTP:

- Для submit, queue-status, run-detail — пишем минимальный HTTP-API в Python (FastAPI) поверх `parts_research_worker`. FastAPI добавляется в зависимости на этом этапе.
- Для куратора — добавляем FastAPI-эндпоинт `POST /curator/message` поверх `parts_research_curator` (тонкая обвязка над `Runner.run_streamed`, стримит SSE). Next.js route handler принимает сообщение пользователя через Vercel AI SDK v6 (`useChat`) и форвардит его сюда, потом стримит события обратно в `useChat`.

Боковая панель добавления задач: поле ввода, кнопка «отправить», валидация на стороне Python через регулярку.

Дашборд очереди: количество по статусам, список карточек, polling раз в пару секунд.

Карточка задачи раскрывается в detail-panel: draft-данные, evidence, `publications`, kit_contents, low_confidence, source_urls.

Страница чата с куратором: сообщения и tool calls стримятся, видно SQL/save_to_smart/mark_needs_review, история сессий доступна как лог.

CLI REPL из этапа 3 не убираем — debug-инструмент.

**Готовность.** В браузере: добавляем партию артикулов, видим карточки в прогрессе, дожидаемся `done`/`needs_human_review`, открываем чат с куратором, просим «обработай очередь», видим стриминг tool calls, проверяем в Smart — записи появились.

---

## Что не делается вообще (вне scope любого этапа)

Зафиксировано в спеке (раздел «Что сейчас не делаем»). Кратко: fuzzy Exa-cache, передача старых research-results, workflow engine, авто-финализация, обязательные UI-подтверждения destructive SQL, raw evidence внутри Smart, отношение к плагинам как к истине, требование полного состава перед публикацией, флаги `has_missing_article` и `is_kit` в Smart, прод-Smart-миграция, версионирование промптов, heartbeat для worker'а, авто-ретраи failure-статусов, retry-стратегии, похожие записи в Smart-плагине, приоритеты в очереди, MCP-серверы для любых агентов, дополнительные фазы 1.5, authentication/мультиюзер, raw token-deltas в `agent_stream_events`, детерминированный merge JSON между turn'ами, auto-обработка пустых обязательных полей в фазе 2.

Если в процессе работы какой-то из этих пунктов окажется реально нужным — обсуждаем отдельно, добавляем в спеку и в план, после чего реализуем.
