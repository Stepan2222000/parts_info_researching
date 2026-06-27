-- ---------------------------------------------------------------------------
-- 010 — FDW к ebay_data и ebay_validation_item: подмешивание валидных
-- eBay-объявлений в research-контекст как ПОДСКАЗКИ (не истины).
--
-- Обе БД — контейнеры в общей docker-сети db_default, доступны по именам на
-- внутреннем порту 5432 (как smart_test/brands_mapping в 001_init.sql). Тот же
-- admin/Password123. READ-ONLY: пишем в эти схемы мы не будем.
--
-- Цепочка (см. context.ebay_listings_lookup):
--   smart_payload['id'] (= smart.parts.id = smart_test, см. 001) ──┐
--   ebay_validation_item.validation_results.smart_id == тот же id ──┘ (id-про-
--     странство smart_test и prod ОБЩЕЕ, проверено — совпадает 100%)
--   status='pass' ⋈ ebay_data.items (INNER: ~2% pass-item исчезли из ebay_data,
--     рассинхрон evi — INNER их молча отбрасывает) ⋈ blobs (description).
--
-- Импортируем ТОЛЬКО нужные таблицы (LIMIT TO): ebay_data.changes партициони-
-- рована помесячно и тут не нужна — её не тянем.
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

DROP SERVER IF EXISTS ebay_data_fdw CASCADE;
CREATE SERVER ebay_data_fdw
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host 'ebay_data', port '5432', dbname 'ebay_data', connect_timeout '5');

DROP SERVER IF EXISTS ebay_validation_item_fdw CASCADE;
CREATE SERVER ebay_validation_item_fdw
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host 'ebay_validation_item', port '5432', dbname 'ebay_validation_item', connect_timeout '5');

CREATE USER MAPPING IF NOT EXISTS FOR admin
    SERVER ebay_data_fdw
    OPTIONS (user 'admin', password 'Password123');

CREATE USER MAPPING IF NOT EXISTS FOR admin
    SERVER ebay_validation_item_fdw
    OPTIONS (user 'admin', password 'Password123');

CREATE SCHEMA IF NOT EXISTS ebay_data;
CREATE SCHEMA IF NOT EXISTS ebay_validation_item;

DROP FOREIGN TABLE IF EXISTS ebay_data.items           CASCADE;
DROP FOREIGN TABLE IF EXISTS ebay_data.item_specifics  CASCADE;
DROP FOREIGN TABLE IF EXISTS ebay_data.blobs           CASCADE;
DROP FOREIGN TABLE IF EXISTS ebay_data.fields          CASCADE;
DROP FOREIGN TABLE IF EXISTS ebay_validation_item.validation_results CASCADE;

IMPORT FOREIGN SCHEMA public
    LIMIT TO (items, item_specifics, blobs, fields)
    FROM SERVER ebay_data_fdw INTO ebay_data;

IMPORT FOREIGN SCHEMA public
    LIMIT TO (validation_results)
    FROM SERVER ebay_validation_item_fdw INTO ebay_validation_item;
