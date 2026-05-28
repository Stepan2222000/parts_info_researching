-- ============================================================================
-- parts_research initial schema (Python rebuild)
-- PostgreSQL 18
-- ============================================================================
-- Единственный источник правды по схеме. Этапы из IMPLEMENTATION_PLAN.md
-- добавляют новые таблицы редактированием этого же файла; миграции
-- применяются вручную (psql или asyncpg.execute), без ORM-миграторов.
--
-- Хранилище — только Postgres, файлов на диске нет:
--   * финальный JSON каждого run'а     -> task_runs.result_json
--   * история input-items Agents SDK    -> agent_history (PostgresSession)
--   * высокоуровневые stream-события     -> agent_stream_events
--   * raw Exa-ответы                     -> exa_cache.response
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Чистый перезапуск (для разработки).
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS publications         CASCADE;
DROP TABLE IF EXISTS agent_sql_log        CASCADE;
DROP TABLE IF EXISTS curator_messages     CASCADE;
DROP TABLE IF EXISTS curator_sessions     CASCADE;
DROP TABLE IF EXISTS plugin_payloads      CASCADE;
DROP TABLE IF EXISTS agent_stream_events  CASCADE;
DROP TABLE IF EXISTS agent_history        CASCADE;
DROP TABLE IF EXISTS exa_cache_usage      CASCADE;
DROP TABLE IF EXISTS exa_cache            CASCADE;
DROP TABLE IF EXISTS draft_part_of_kits   CASCADE;
DROP TABLE IF EXISTS draft_kit_components CASCADE;
DROP TABLE IF EXISTS draft_part_articles  CASCADE;
DROP TABLE IF EXISTS draft_parts          CASCADE;
DROP TABLE IF EXISTS task_runs            CASCADE;
DROP TABLE IF EXISTS tasks                CASCADE;
DROP TYPE  IF EXISTS curator_role;
DROP TYPE  IF EXISTS article_confidence;
DROP TYPE  IF EXISTS task_run_status;

-- ---------------------------------------------------------------------------
-- Типы.
-- ---------------------------------------------------------------------------
CREATE TYPE task_run_status AS ENUM (
    'queued',
    'running',
    'done',
    'failed_no_data',
    'failed_validation',
    'failed_crashed',
    'needs_human_review'
);

CREATE TYPE article_confidence AS ENUM (
    'confirmed',
    'low_confidence',
    'irrelevant'
);

CREATE TYPE curator_role AS ENUM ('user', 'assistant', 'tool');

-- ---------------------------------------------------------------------------
-- tasks: одна логическая задача на один артикул.
-- ---------------------------------------------------------------------------
CREATE TABLE tasks (
    id         BIGSERIAL PRIMARY KEY,
    article    TEXT NOT NULL CHECK (article ~ '^[A-Z0-9\-]+$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tasks_article ON tasks(article);

COMMENT ON TABLE  tasks         IS 'Задачи ресерча. Один submit одного артикула = одна task.';
COMMENT ON COLUMN tasks.article IS 'Входной артикул после нормализации: только [A-Z0-9-].';

-- ---------------------------------------------------------------------------
-- task_runs: конкретный запуск. История запусков по task сохраняется.
-- ---------------------------------------------------------------------------
CREATE TABLE task_runs (
    id          BIGSERIAL PRIMARY KEY,
    task_id     BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status      task_run_status NOT NULL,
    result_json JSONB,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX idx_task_runs_task   ON task_runs(task_id);
CREATE INDEX idx_task_runs_status ON task_runs(status);
CREATE INDEX idx_task_runs_queued ON task_runs(id) WHERE status = 'queued';

COMMENT ON TABLE  task_runs             IS 'Запуски ресерча.';
COMMENT ON COLUMN task_runs.result_json IS 'Финальный StructuredResult run-а целиком (JSONB).';
COMMENT ON COLUMN task_runs.created_at  IS 'Когда запуск создан (в очереди).';
COMMENT ON COLUMN task_runs.started_at  IS 'Когда worker реально начал работу. NULL пока queued.';
COMMENT ON COLUMN task_runs.error       IS 'Текст ошибки для failed_*/needs_human_review.';

-- ---------------------------------------------------------------------------
-- agent_history: input-items Agents SDK Session (PostgresSession).
-- Одна строка = один item. Порядок — по id. session_id вида
-- 'research_run_<run_id>' или 'curator_<session_id>'.
-- ---------------------------------------------------------------------------
CREATE TABLE agent_history (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    item       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_agent_history_session ON agent_history(session_id, id);

COMMENT ON TABLE  agent_history      IS 'История input-items для Agents SDK Session (research и curator).';
COMMENT ON COLUMN agent_history.item IS 'Один TResponseInputItem (user/assistant/tool/reasoning) как JSONB.';

-- ---------------------------------------------------------------------------
-- agent_stream_events: высокоуровневые stream-события каждого run'а.
-- Только RunItemStreamEvent и AgentUpdatedStreamEvent, без raw token-deltas.
-- ---------------------------------------------------------------------------
CREATE TABLE agent_stream_events (
    id         BIGSERIAL PRIMARY KEY,
    run_id     BIGINT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    turn_idx   INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    event      JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_agent_stream_events_run ON agent_stream_events(run_id, turn_idx, seq);

COMMENT ON TABLE  agent_stream_events     IS 'Высокоуровневые streaming-события run-а (без raw token-deltas).';
COMMENT ON COLUMN agent_stream_events.seq IS 'Порядковый номер события внутри turn_idx.';

-- ---------------------------------------------------------------------------
-- draft_parts: распарсенный итоговый JSON.
-- ---------------------------------------------------------------------------
CREATE TABLE draft_parts (
    id                   BIGSERIAL PRIMARY KEY,
    run_id               BIGINT NOT NULL UNIQUE REFERENCES task_runs(id) ON DELETE CASCADE,
    name                 TEXT,
    brand_oem            TEXT[] NOT NULL DEFAULT '{}',
    product_type         TEXT,
    description          TEXT,
    is_kit               BOOLEAN NOT NULL,
    weight_kg            NUMERIC(10,4),
    weight_source_url    TEXT,
    weight_evidence      TEXT,
    models_text          TEXT,
    models_source_urls   TEXT[],
    models_evidence      TEXT,
    needs_review_reason  TEXT
);

COMMENT ON COLUMN draft_parts.brand_oem           IS 'Массив Smart-брендов. Пусто = агент не определил.';
COMMENT ON COLUMN draft_parts.product_type        IS 'Один из smart.product_types.name. NULL = не определён, run -> needs_human_review.';
COMMENT ON COLUMN draft_parts.needs_review_reason IS 'Причина needs_human_review. NULL для done и failed_*.';

-- ---------------------------------------------------------------------------
-- draft_part_articles
-- ---------------------------------------------------------------------------
CREATE TABLE draft_part_articles (
    id                 BIGSERIAL PRIMARY KEY,
    draft_part_id      BIGINT NOT NULL REFERENCES draft_parts(id) ON DELETE CASCADE,
    article            TEXT NOT NULL,
    confidence         article_confidence NOT NULL,
    source_url         TEXT NOT NULL,
    evidence           TEXT NOT NULL,
    why_low_confidence TEXT,
    why_irrelevant     TEXT
);

CREATE INDEX idx_draft_part_articles_draft   ON draft_part_articles(draft_part_id);
CREATE INDEX idx_draft_part_articles_article ON draft_part_articles(article);

-- ---------------------------------------------------------------------------
-- draft_kit_components / draft_part_of_kits
-- ---------------------------------------------------------------------------
CREATE TABLE draft_kit_components (
    id            BIGSERIAL PRIMARY KEY,
    draft_part_id BIGINT NOT NULL REFERENCES draft_parts(id) ON DELETE CASCADE,
    component_key TEXT NOT NULL,
    article       TEXT,
    name          TEXT,
    quantity      INTEGER,
    description   TEXT,
    source_url    TEXT NOT NULL,
    evidence      TEXT NOT NULL
);

CREATE INDEX idx_draft_kit_components_draft ON draft_kit_components(draft_part_id);

COMMENT ON COLUMN draft_kit_components.component_key IS 'article если не null, иначе unknown_1, unknown_2, ... в порядке появления. Генерит backend.';

CREATE TABLE draft_part_of_kits (
    id            BIGSERIAL PRIMARY KEY,
    draft_part_id BIGINT NOT NULL REFERENCES draft_parts(id) ON DELETE CASCADE,
    kit_article   TEXT,
    kit_name      TEXT,        -- nullable: модель может не знать имя набора
    source_url    TEXT NOT NULL,
    evidence      TEXT NOT NULL
);

CREATE INDEX idx_draft_part_of_kits_draft ON draft_part_of_kits(draft_part_id);

-- ---------------------------------------------------------------------------
-- exa_cache: exact-match по (tool_name, args без run_id). Хранится СЫРОЙ
-- ответ Exa целиком; pick/обрезка применяются на чтении.
-- ---------------------------------------------------------------------------
CREATE TABLE exa_cache (
    id            BIGSERIAL PRIMARY KEY,
    request_hash  TEXT NOT NULL UNIQUE,
    tool_name     TEXT NOT NULL,
    arguments     JSONB NOT NULL,
    response      JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    hit_count     INTEGER NOT NULL DEFAULT 0
);

COMMENT ON TABLE  exa_cache              IS 'Глобальный exact-match кэш Exa-вызовов (сырой ответ).';
COMMENT ON COLUMN exa_cache.request_hash IS 'sha256 от tool_name + canonical JSON аргументов (без run_id).';

-- ---------------------------------------------------------------------------
-- exa_cache_usage: какой run какой кэш-ответ использовал, в какой фазе.
-- ---------------------------------------------------------------------------
CREATE TABLE exa_cache_usage (
    id        BIGSERIAL PRIMARY KEY,
    cache_id  BIGINT NOT NULL REFERENCES exa_cache(id) ON DELETE CASCADE,
    run_id    BIGINT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    phase     TEXT,
    hit       BOOLEAN NOT NULL,
    used_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_exa_cache_usage_run   ON exa_cache_usage(run_id);
CREATE INDEX idx_exa_cache_usage_cache ON exa_cache_usage(cache_id);

COMMENT ON COLUMN exa_cache_usage.phase IS 'main | low_confidence | kit_contents | agent_extra.';
COMMENT ON COLUMN exa_cache_usage.hit   IS 'TRUE — взято из кэша, FALSE — впервые попало в кэш этим run-ом.';

-- ---------------------------------------------------------------------------
-- plugin_payloads: данные от source-плагинов, привязанные к run.
-- ---------------------------------------------------------------------------
CREATE TABLE plugin_payloads (
    id          BIGSERIAL PRIMARY KEY,
    run_id      BIGINT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    plugin_name TEXT NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_plugin_payloads_run ON plugin_payloads(run_id);

COMMENT ON TABLE plugin_payloads IS 'Данные source-плагинов (Smart, в будущем другие).';

-- ---------------------------------------------------------------------------
-- curator_sessions: одна строка на сессию (CLI REPL или вкладка в UI).
-- ---------------------------------------------------------------------------
CREATE TABLE curator_sessions (
    id         BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at   TIMESTAMPTZ
);

COMMENT ON TABLE curator_sessions IS 'Сессии куратора. История чата — в agent_history(session_id=curator_<id>).';

-- ---------------------------------------------------------------------------
-- curator_messages: визуальная история чата + tool calls.
-- ---------------------------------------------------------------------------
CREATE TABLE curator_messages (
    id         BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES curator_sessions(id) ON DELETE CASCADE,
    role       curator_role NOT NULL,
    content    TEXT,
    tool_call  JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_curator_messages_session ON curator_messages(session_id, id);

COMMENT ON COLUMN curator_messages.content   IS 'Текст user/assistant. Для role=tool — NULL.';
COMMENT ON COLUMN curator_messages.tool_call IS 'Для role=tool — полный tool-call item (tool/arguments/result/error/status).';

-- ---------------------------------------------------------------------------
-- agent_sql_log: запись логируется ДО выполнения, потом UPDATE результатом.
-- ---------------------------------------------------------------------------
CREATE TABLE agent_sql_log (
    id            BIGSERIAL PRIMARY KEY,
    session_id    BIGINT NOT NULL REFERENCES curator_sessions(id) ON DELETE CASCADE,
    sql_text      TEXT NOT NULL,
    rows_affected INTEGER,
    error         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE INDEX idx_agent_sql_log_session ON agent_sql_log(session_id, id);

-- ---------------------------------------------------------------------------
-- publications: трассировка run -> Smart-запись.
-- ---------------------------------------------------------------------------
CREATE TABLE publications (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             BIGINT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    curator_session_id BIGINT NOT NULL REFERENCES curator_sessions(id) ON DELETE CASCADE,
    smart_id           TEXT NOT NULL,
    published_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_publications_run     ON publications(run_id);
CREATE INDEX idx_publications_session ON publications(curator_session_id);

COMMENT ON TABLE publications IS 'Одна строка на каждый успешный save_to_smart по одному part. smart_id — id записи в smart.parts.';

-- ---------------------------------------------------------------------------
-- FDW: smart_test и brands_mapping.
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

DROP SERVER IF EXISTS smart_fdw CASCADE;
CREATE SERVER smart_fdw
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host 'smart_test', port '5432', dbname 'smart_test', batch_size '50');

DROP SERVER IF EXISTS brand_mapping_fdw CASCADE;
CREATE SERVER brand_mapping_fdw
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host 'brands_mapping', port '5432', dbname 'brands_mapping');

CREATE USER MAPPING IF NOT EXISTS FOR admin
    SERVER smart_fdw
    OPTIONS (user 'admin', password 'Password123');

CREATE USER MAPPING IF NOT EXISTS FOR admin
    SERVER brand_mapping_fdw
    OPTIONS (user 'admin', password 'Password123');

CREATE SCHEMA IF NOT EXISTS smart;
CREATE SCHEMA IF NOT EXISTS brand_mapping;

DROP FOREIGN TABLE IF EXISTS smart.parts            CASCADE;
DROP FOREIGN TABLE IF EXISTS smart.brands           CASCADE;
DROP FOREIGN TABLE IF EXISTS smart.part_brands      CASCADE;
DROP FOREIGN TABLE IF EXISTS smart.part_articles    CASCADE;
DROP FOREIGN TABLE IF EXISTS smart.part_components  CASCADE;
DROP FOREIGN TABLE IF EXISTS smart.product_types    CASCADE;

IMPORT FOREIGN SCHEMA public
    LIMIT TO (parts, brands, part_brands, part_articles, part_components, product_types)
    FROM SERVER smart_fdw INTO smart;

IMPORT FOREIGN SCHEMA public
    FROM SERVER brand_mapping_fdw INTO brand_mapping;
