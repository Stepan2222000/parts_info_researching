-- 009: difference-turn — нюансы между кросс-номерами одной детали.
-- Финальный turn ресёрча пишет: пер-артикульные заметки (note), границы всей
-- детали (part_caveats) и порядок замен с пруфом (supersession). Всё с
-- source_url+evidence; только genuine-OEM/дилер/форум (aftermarket запрещён).

BEGIN;

-- Пер-артикульная заметка-нюанс (Gen2-внутренности, needs-reflash и т.п.).
-- Только для confirmed-номеров; для остальных NULL.
ALTER TABLE draft_part_articles
    ADD COLUMN note_text       TEXT,
    ADD COLUMN note_source_url TEXT,
    ADD COLUMN note_evidence   TEXT;

COMMENT ON COLUMN draft_part_articles.note_text IS
    'Difference-turn: нюанс по этому номеру (с note_source_url+note_evidence). NULL = нет.';

-- Границы/нюансы ВСЕЙ детали (wet-joint vs dry-joint, freshwater-only, …).
CREATE TABLE draft_part_caveats (
    id            BIGSERIAL PRIMARY KEY,
    draft_part_id BIGINT NOT NULL REFERENCES draft_parts(id) ON DELETE CASCADE,
    caveat        TEXT NOT NULL,
    source_url    TEXT NOT NULL,
    evidence      TEXT NOT NULL
);
CREATE INDEX idx_draft_part_caveats_draft ON draft_part_caveats(draft_part_id);

-- Цепочка замен: newer заменяет older (порядок новое→старое), с пруфом на ребро.
CREATE TABLE draft_supersession (
    id            BIGSERIAL PRIMARY KEY,
    draft_part_id BIGINT NOT NULL REFERENCES draft_parts(id) ON DELETE CASCADE,
    newer         TEXT NOT NULL,
    older         TEXT NOT NULL,
    source_url    TEXT NOT NULL,
    evidence      TEXT NOT NULL
);
CREATE INDEX idx_draft_supersession_draft ON draft_supersession(draft_part_id);

COMMIT;
