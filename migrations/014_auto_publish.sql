-- 014: авто-публикация (режим «сразу в smart после ресёрча, без куратора»).
--
-- publications: строка теперь может быть создана не куратором, а авто-режимом
-- воркера. curator_session_id становится nullable; published_by различает
-- источник ('curator' | 'auto'). CHECK гарантирует, что кураторская строка
-- всегда со своей сессией (не врём в данных).
--
-- task_runs.auto_publish_outcome: исход попытки авто-публикации этого рана,
-- jsonb {"decision": "published"|"skipped"|"error", ...} — причина пропуска /
-- текст ошибки видны потребителю и куратору (разбор «что авто-режим отложил»
-- одним SQL-фильтром). NULL = авто-публикация к рану не применялась.

ALTER TABLE publications ALTER COLUMN curator_session_id DROP NOT NULL;
ALTER TABLE publications ADD COLUMN published_by TEXT NOT NULL DEFAULT 'curator';
ALTER TABLE publications ADD CONSTRAINT publications_published_by_check
    CHECK (published_by IN ('curator', 'auto'));
ALTER TABLE publications ADD CONSTRAINT publications_curator_has_session
    CHECK (published_by <> 'curator' OR curator_session_id IS NOT NULL);

ALTER TABLE task_runs ADD COLUMN auto_publish_outcome JSONB;

COMMENT ON COLUMN publications.published_by IS
    'Источник публикации: curator (тул save_to_smart из сессии куратора) | auto (авто-режим воркера после ресёрча)';
COMMENT ON COLUMN task_runs.auto_publish_outcome IS
    'Исход авто-публикации рана: {"decision": "published"|"skipped"|"error", ...}; NULL = авто-режим не применялся';
