-- ============================================================================
-- parts_research initial schema
-- PostgreSQL 18
-- ============================================================================
-- Единственный источник правды по схеме. Этапы из IMPLEMENTATION_PLAN.md
-- добавляют новые таблицы редактированием этого же файла; миграции
-- применяются psql-ом вручную.
--
-- На этапе 2: добавлены exa_cache, exa_cache_usage, plugin_payloads;
-- brand_oem стал массивом; product_type и needs_review_reason появились;
-- task_runs.started_at стал nullable (queued/running разделены явно).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Чистый перезапуск (для разработки).
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS plugin_payloads     CASCADE;
DROP TABLE IF EXISTS exa_cache_usage     CASCADE;
DROP TABLE IF EXISTS exa_cache           CASCADE;
DROP TABLE IF EXISTS draft_part_of_kits  CASCADE;
DROP TABLE IF EXISTS draft_kit_components CASCADE;
DROP TABLE IF EXISTS draft_part_articles CASCADE;
DROP TABLE IF EXISTS draft_parts         CASCADE;
DROP TABLE IF EXISTS task_runs           CASCADE;
DROP TABLE IF EXISTS tasks               CASCADE;
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
    id              BIGSERIAL PRIMARY KEY,
    task_id         BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status          task_run_status NOT NULL,
    codex_thread_id TEXT,
    storage_dir     TEXT,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);

CREATE INDEX idx_task_runs_task       ON task_runs(task_id);
CREATE INDEX idx_task_runs_status     ON task_runs(status);
CREATE INDEX idx_task_runs_queued     ON task_runs(id) WHERE status = 'queued';

COMMENT ON TABLE  task_runs                 IS 'Запуски ресерча.';
COMMENT ON COLUMN task_runs.codex_thread_id IS 'thread_id из @openai/codex-sdk.';
COMMENT ON COLUMN task_runs.storage_dir     IS 'Относительный путь к storage/runs/{run_id}/.';
COMMENT ON COLUMN task_runs.created_at      IS 'Когда запуск создан (в очереди).';
COMMENT ON COLUMN task_runs.started_at      IS 'Когда worker реально начал работу. NULL пока queued.';
COMMENT ON COLUMN task_runs.error           IS 'Текст ошибки для failed_*/needs_human_review.';

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

COMMENT ON COLUMN draft_parts.brand_oem           IS 'Массив Smart-брендов (BRP/MERCRUISER/...). Пусто = агент не определил.';
COMMENT ON COLUMN draft_parts.product_type        IS 'Один из smart.product_types.name. NULL = агент не определил, run → needs_human_review.';
COMMENT ON COLUMN draft_parts.needs_review_reason IS 'Причина needs_human_review, если run в этот статус. NULL для done и failed_*.';

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

CREATE TABLE draft_part_of_kits (
    id            BIGSERIAL PRIMARY KEY,
    draft_part_id BIGINT NOT NULL REFERENCES draft_parts(id) ON DELETE CASCADE,
    kit_article   TEXT,
    kit_name      TEXT NOT NULL,
    source_url    TEXT NOT NULL,
    evidence      TEXT NOT NULL
);

CREATE INDEX idx_draft_part_of_kits_draft ON draft_part_of_kits(draft_part_id);

-- ---------------------------------------------------------------------------
-- exa_cache: exact-match по (tool_name, args без run_id).
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

COMMENT ON TABLE  exa_cache              IS 'Глобальный exact-match кэш Exa-вызовов.';
COMMENT ON COLUMN exa_cache.request_hash IS 'sha256 от tool_name + canonical JSON аргументов (без run_id).';
COMMENT ON COLUMN exa_cache.hit_count    IS 'Сколько раз ответ переиспользовался из кэша.';

-- ---------------------------------------------------------------------------
-- exa_cache_usage: какой run какой кэш-ответ использовал.
-- ---------------------------------------------------------------------------
CREATE TABLE exa_cache_usage (
    id        BIGSERIAL PRIMARY KEY,
    cache_id  BIGINT NOT NULL REFERENCES exa_cache(id) ON DELETE CASCADE,
    run_id    BIGINT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    used_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    hit       BOOLEAN NOT NULL
);

CREATE INDEX idx_exa_cache_usage_run   ON exa_cache_usage(run_id);
CREATE INDEX idx_exa_cache_usage_cache ON exa_cache_usage(cache_id);

COMMENT ON COLUMN exa_cache_usage.hit IS 'TRUE — взято из кэша, FALSE — впервые попало в кэш этим run-ом.';

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

COMMENT ON TABLE plugin_payloads IS 'Данные source-плагинов (Smart, в будущем Avito и т.п.).';

-- ---------------------------------------------------------------------------
-- FDW: smart_test и brands_mapping.
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

DROP SERVER IF EXISTS smart_fdw CASCADE;
CREATE SERVER smart_fdw
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host 'smart_test', port '5432', dbname 'smart_test');

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
