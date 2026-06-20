-- ============================================================================
-- 005 — английские тексты (parts_en) и цены за оригинал (parts_prices).
--
-- Контекст: smart теперь хранит и русские (smart.parts), и английские
-- (public.parts_en) name/description; цены живут в отдельной БД parts_prices
-- (схема market, журнал observations + функции record_price/min_price).
--
-- 1) smart.parts_en — foreign table через smart_fdw (EN-зеркало smart.parts).
-- 2) draft_parts / draft_kit_components — английские name/description от агента.
-- 3) draft_prices — найденные research-агентом US-цены за оригинал; при
--    публикации уезжают в parts_prices.market.record_price(smart_id, ...).
--
-- parts_prices НЕ через FDW: запись идёт прямым подключением из save_to_smart
-- (другая БД, нельзя в одной транзакции со smart-FDW).
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1) smart.parts_en — EN-зеркало (public.parts_en на удалённом smart).
--    part_id PK -> smart.parts.id (ON DELETE CASCADE на стороне smart).
-- ---------------------------------------------------------------------------
DROP FOREIGN TABLE IF EXISTS smart.parts_en CASCADE;
CREATE FOREIGN TABLE smart.parts_en (
    part_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT
) SERVER smart_fdw OPTIONS (schema_name 'public', table_name 'parts_en');

COMMENT ON FOREIGN TABLE smart.parts_en IS
    'EN-зеркало smart.parts (public.parts_en на удалённом smart). '
    'part_id -> smart.parts.id. name NOT NULL, description nullable.';

-- ---------------------------------------------------------------------------
-- 2) Английские name/description в драфтах (research-агент пишет оба языка).
-- ---------------------------------------------------------------------------
ALTER TABLE draft_parts ADD COLUMN name_en        TEXT;
ALTER TABLE draft_parts ADD COLUMN description_en TEXT;

COMMENT ON COLUMN draft_parts.name_en        IS 'Английское имя от research-агента (-> smart.parts_en.name). NULL = не определено.';
COMMENT ON COLUMN draft_parts.description_en IS 'Английское описание (-> smart.parts_en.description).';

ALTER TABLE draft_kit_components ADD COLUMN name_en        TEXT;
ALTER TABLE draft_kit_components ADD COLUMN description_en TEXT;

COMMENT ON COLUMN draft_kit_components.name_en        IS 'Английское имя компонента (-> smart.parts_en при INSERT компонента).';
COMMENT ON COLUMN draft_kit_components.description_en IS 'Английское описание компонента.';

-- ---------------------------------------------------------------------------
-- 3) draft_prices — US-цены за оригинал, найденные research-агентом.
--    Одна строка = один оффер (магазин). При публикации пишутся в
--    parts_prices.market.record_price(smart_id, site, price, currency, url, note).
-- ---------------------------------------------------------------------------
CREATE TABLE draft_prices (
    id          BIGSERIAL PRIMARY KEY,
    run_id      BIGINT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    article     TEXT NOT NULL,                       -- номер, по которому найдена цена (-> note)
    site        TEXT NOT NULL,                       -- магазин (домен), ключ market.sites
    price       NUMERIC(12,2) NOT NULL CHECK (price > 0),
    currency    TEXT NOT NULL DEFAULT 'USD',
    url         TEXT,                                -- ссылка на товарную страницу
    in_stock    BOOLEAN,
    evidence    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_draft_prices_run ON draft_prices(run_id);

COMMENT ON TABLE  draft_prices         IS 'US-цены за оригинал, найденные research-агентом. При публикации -> parts_prices.market.record_price(smart_id, ...).';
COMMENT ON COLUMN draft_prices.article IS 'OEM-номер, по которому найдена цена (пишется в note observation).';
COMMENT ON COLUMN draft_prices.site    IS 'Магазин (домен), ключ market.sites; auto-создаётся record_price.';

COMMIT;
