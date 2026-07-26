-- ============================================================================
-- 016 — авто-курация очереди (см. auto_curation.md).
--
-- 1) draft_parts.classes_backfill — след разового дозаполнения vehicle_classes
--    дешёвой LLM для «майских» ранов (контракт до миграции 004 классов не имел).
--    NULL = классы от research-агента; JSONB = {model, at, confident} бэкфилла.
--
-- 2) dedup_candidates — очередь «возможных дублей» от воронки похожести:
--    после auto-INSERT новой записи в smart детерминированный шорт-лист топ-K
--    прогоняется через LLM-судью; вердикты same/likely_same складываются сюда.
--    Публикацию кандидаты НЕ блокируют; слияние — только LLM-куратор с
--    пруф-URL (см. auto_curation.md, «Поиск похожих — анти-дубли»).
-- ============================================================================

BEGIN;

ALTER TABLE draft_parts ADD COLUMN classes_backfill JSONB;
COMMENT ON COLUMN draft_parts.classes_backfill IS
    'NULL = vehicle_classes от research-агента. JSONB {model, at, confident} — '
    'классы дозаполнены разовым LLM-бэкфиллом (cli.backfill_classes).';

CREATE TABLE dedup_candidates (
    id                 BIGSERIAL PRIMARY KEY,
    publication_id     BIGINT NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    run_id             BIGINT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    smart_id           TEXT NOT NULL,   -- только что опубликованная запись
    candidate_smart_id TEXT NOT NULL,   -- существующая запись, похожая на неё
    verdict            TEXT NOT NULL CHECK (verdict IN ('same', 'likely_same')),
    reason             TEXT NOT NULL,   -- обоснование судьи
    score              REAL NOT NULL,   -- мягкий скор воронки (ранжирование шорт-листа)
    judge_model        TEXT NOT NULL,   -- какая модель вынесла вердикт
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'merged', 'kept_separate', 'dismissed')),
    proof_urls         TEXT[],          -- пруфы куратора при merged (склейка без общего номера)
    resolution_note    TEXT,            -- чем кончился разбор
    resolved_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (publication_id, candidate_smart_id)
);
CREATE INDEX idx_dedup_candidates_open ON dedup_candidates(status) WHERE status = 'open';
CREATE INDEX idx_dedup_candidates_run ON dedup_candidates(run_id);

COMMENT ON TABLE dedup_candidates IS
    'Гипотезы «опубликованная запись — дубль существующей» от воронки похожести '
    '(шорт-лист + LLM-судья). Разбирает LLM-куратор: merged только с proof_urls; '
    'нет пруфа — kept_separate (дубль дешевле ложной склейки).';

COMMIT;
