-- ============================================================================
-- parts_research initial schema
-- PostgreSQL 18
-- ============================================================================
-- Этот файл — единственный источник правды по схеме. По мере роста системы
-- (этапы 2 и 3 из IMPLEMENTATION_PLAN.md) сюда добавляются новые таблицы
-- редактированием этого же файла. Миграции применяются через psql вручную.
--
-- Сейчас здесь — всё, что нужно для этапа 1: tasks, runs, draft-таблицы для
-- одного research-run. FDW-объекты тоже здесь, чтобы DDL был самодостаточным.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Чистый перезапуск (для разработки).
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS draft_part_of_kits CASCADE;
DROP TABLE IF EXISTS draft_kit_components CASCADE;
DROP TABLE IF EXISTS draft_part_articles CASCADE;
DROP TABLE IF EXISTS draft_parts CASCADE;
DROP TABLE IF EXISTS task_runs CASCADE;
DROP TABLE IF EXISTS tasks CASCADE;
DROP TYPE IF EXISTS article_confidence;
DROP TYPE IF EXISTS task_run_status;

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
-- Повторный запуск по тому же артикулу создаёт НОВУЮ task (см. план, этап 1).
-- ---------------------------------------------------------------------------
CREATE TABLE tasks (
    id         BIGSERIAL PRIMARY KEY,
    article    TEXT NOT NULL CHECK (article ~ '^[A-Z0-9\-]+$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tasks_article ON tasks(article);

COMMENT ON TABLE  tasks            IS 'Задачи ресерча. Один артикул = одна task (на этапе 1, без дедупликации).';
COMMENT ON COLUMN tasks.article    IS 'Входной артикул после нормализации: только [A-Z0-9-].';

-- ---------------------------------------------------------------------------
-- task_runs: конкретный запуск ресерча. У одной task может быть несколько
-- runs (история перезапусков).
-- ---------------------------------------------------------------------------
CREATE TABLE task_runs (
    id              BIGSERIAL PRIMARY KEY,
    task_id         BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status          task_run_status NOT NULL,
    codex_thread_id TEXT,
    storage_dir     TEXT,
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);

CREATE INDEX idx_task_runs_task   ON task_runs(task_id);
CREATE INDEX idx_task_runs_status ON task_runs(status);

COMMENT ON TABLE  task_runs                 IS 'Запуски ресерча. История запусков по одной task сохраняется.';
COMMENT ON COLUMN task_runs.codex_thread_id IS 'thread_id из @openai/codex-sdk, для аудита.';
COMMENT ON COLUMN task_runs.storage_dir     IS 'Относительный путь к storage/runs/{run_id}/.';
COMMENT ON COLUMN task_runs.error           IS 'Сообщение об ошибке для failed_*-статусов.';

-- ---------------------------------------------------------------------------
-- draft_parts: итог парсинга финального Codex JSON в нормальную форму.
-- На этапе 1 evidence хранится прямо здесь как denormalized колонки.
-- На этапе 2 evidence переедет в отдельную таблицу.
-- ---------------------------------------------------------------------------
CREATE TABLE draft_parts (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              BIGINT NOT NULL UNIQUE REFERENCES task_runs(id) ON DELETE CASCADE,
    name                TEXT,
    brand_oem           TEXT,
    description         TEXT,
    is_kit              BOOLEAN NOT NULL,
    weight_kg           NUMERIC(10,4),
    weight_source_url   TEXT,
    weight_evidence     TEXT,
    models_text         TEXT,
    models_source_urls  TEXT[],
    models_evidence     TEXT
);

COMMENT ON TABLE  draft_parts          IS 'Draft-результат research-run`а, по одной строке на run.';
COMMENT ON COLUMN draft_parts.brand_oem IS 'Бренд как вернул агент. На этапе 1 — одиночная строка. На этапе 2 станет массивом.';
COMMENT ON COLUMN draft_parts.is_kit    IS 'Что агент сказал. Реальная kit-ность определяется по draft_kit_components.';

-- ---------------------------------------------------------------------------
-- draft_part_articles: артикулы из numbers.{article,article_low_confidence,irrelevant}.
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

COMMENT ON TABLE draft_part_articles IS
    'Найденные артикулы, разбитые по confidence (confirmed / low_confidence / irrelevant).';

-- ---------------------------------------------------------------------------
-- draft_kit_components: состав набора. Компонент может быть с артикулом или
-- без (тогда article = NULL, component_key = "unknown_N").
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

COMMENT ON COLUMN draft_kit_components.component_key IS
    'Ключ компонента из JSON: либо артикул, либо "unknown_N", если артикул не найден.';

-- ---------------------------------------------------------------------------
-- draft_part_of_kits: наборы, в которые входит исследуемый артикул.
-- ---------------------------------------------------------------------------
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
-- FDW: smart_test и brands_mapping.
-- Контейнеры висят в docker-сети db_default на сервере, поэтому host —
-- имя контейнера (smart_test / brands_mapping), порт — внутренний 5432.
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

-- При повторном применении IMPORT упадёт на существующих таблицах, поэтому
-- сбрасываем foreign tables и заново тянем актуальную форму удалённых схем.
DROP FOREIGN TABLE IF EXISTS smart.parts            CASCADE;
DROP FOREIGN TABLE IF EXISTS smart.brands           CASCADE;
DROP FOREIGN TABLE IF EXISTS smart.part_brands      CASCADE;
DROP FOREIGN TABLE IF EXISTS smart.part_articles    CASCADE;
DROP FOREIGN TABLE IF EXISTS smart.part_components  CASCADE;
DROP FOREIGN TABLE IF EXISTS smart.product_types    CASCADE;

IMPORT FOREIGN SCHEMA public
    LIMIT TO (parts, brands, part_brands, part_articles, part_components, product_types)
    FROM SERVER smart_fdw INTO smart;

-- brands_mapping пока пустая. IMPORT — no-op, оставляем для будущего сидинга.
IMPORT FOREIGN SCHEMA public
    FROM SERVER brand_mapping_fdw INTO brand_mapping;
