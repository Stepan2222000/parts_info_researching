-- 007: формат-валидация артикулов (parts_research).
--
-- Артикулы должны быть в канонической форме smart (по правилам brand_mapping.article_match_rules).
-- Валидатор — в коде (read-only по правилам), сам ничего не правит: при неканоне критически
-- падаем и СОХРАНЯЕМ проблему сюда для последующего анализа/дозаполнения правил.
--
-- Без дублирования: артикул/бренд/run достаются JOIN'ом через
-- draft_part_articles -> draft_parts -> task_runs. Здесь только новое: причина + ожидаемая
-- каноника + какое правило сматчило + где поймали (research/curator).
--
-- Foreign-таблица brand_mapping.article_match_rules уже импортирована (миграция 001);
-- новые колонки example_from/example_to (если применён brands_mapping/001_article_format_validation.sql)
-- читать не обязательно — спеку для модели строим из note+find_regex. При желании их можно
-- подтянуть: ALTER FOREIGN TABLE brand_mapping.article_match_rules
--   ADD COLUMN example_from text, ADD COLUMN example_to text;  (только ПОСЛЕ применения к brands_mapping).

CREATE TABLE article_format_problems (
    id                 BIGSERIAL PRIMARY KEY,
    draft_article_id   BIGINT NOT NULL REFERENCES draft_part_articles(id) ON DELETE CASCADE,
    reason             TEXT   NOT NULL,   -- not_canonical | no_rule | not_in_sources
    expected_canonical TEXT,              -- для not_canonical: что правило считает каноном
    rule_name          TEXT,             -- какое правило сматчило (трассировка); NULL для no_rule
    source             TEXT   NOT NULL,   -- research | curator
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draft_article_id, source)     -- один вердикт на артикул в рамках источника (re-validate = upsert)
);

CREATE INDEX idx_afp_draft_article ON article_format_problems(draft_article_id);
CREATE INDEX idx_afp_reason        ON article_format_problems(reason);

COMMENT ON TABLE  article_format_problems IS
  'Артикулы, не прошедшие формат-валидацию (по brand_mapping.article_match_rules). Контекст без дублей — через FK на draft_part_articles.';
COMMENT ON COLUMN article_format_problems.reason IS
  'not_canonical (правило дало другую канонику) | no_rule (формат не покрыт) | not_in_sources (нет в источниках Exa, fast-follow).';
COMMENT ON COLUMN article_format_problems.expected_canonical IS
  'Для not_canonical — каноника по правилу (что должна была выдать модель).';
