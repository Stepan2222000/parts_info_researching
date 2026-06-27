-- 010: difference-turn v2 — сливаем пер-артикульные note и part_caveats в ОДИН
-- список нюансов с опциональной привязкой к номерам (articles=[] = вся деталь).
-- supersession (порядок) остаётся как есть (теперь только среди confirmed-номеров).

BEGIN;

-- Убираем старое разнесение нюансов на два уровня.
ALTER TABLE draft_part_articles
    DROP COLUMN IF EXISTS note_text,
    DROP COLUMN IF EXISTS note_source_url,
    DROP COLUMN IF EXISTS note_evidence;

DROP TABLE IF EXISTS draft_part_caveats;

-- Единый список нюансов. articles — номера, к которым относится (пусто = вся деталь).
CREATE TABLE draft_nuances (
    id            BIGSERIAL PRIMARY KEY,
    draft_part_id BIGINT NOT NULL REFERENCES draft_parts(id) ON DELETE CASCADE,
    text          TEXT NOT NULL,
    articles      TEXT[] NOT NULL DEFAULT '{}',
    source_url    TEXT NOT NULL,
    evidence      TEXT NOT NULL
);
CREATE INDEX idx_draft_nuances_draft ON draft_nuances(draft_part_id);

COMMENT ON COLUMN draft_nuances.articles IS
    'Номера, к которым относится нюанс. Пусто = ко всей детали.';

COMMIT;
