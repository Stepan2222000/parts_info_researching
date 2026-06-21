-- ============================================================================
-- 006 — parts_prices через FDW: цены пишутся В ТОЙ ЖЕ транзакции, что и
-- smart-публикация (атомарно per-part). Раньше цены шли отдельным подключением
-- ПОСЛЕ коммита (не атомарно) — теперь через foreign-таблицы внутри save_to_smart.
--
-- Нюанс postgres_fdw: при INSERT он шлёт и авто-генерируемые колонки как NULL,
-- поэтому для записи держим отдельные write-FT БЕЗ авто-колонок:
--   market.sites.id          — serial (nextval) -> в sites_w нет id/created_at
--   market.observations.id   — GENERATED ALWAYS AS IDENTITY -> в observations_w нет id/observed_at
-- read-FT market.sites (с id) нужна, чтобы найти site_id по имени.
-- ============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS postgres_fdw;

DROP SERVER IF EXISTS parts_prices_fdw CASCADE;
CREATE SERVER parts_prices_fdw
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host 'parts_prices', port '5432', dbname 'parts_prices');

CREATE USER MAPPING IF NOT EXISTS FOR admin
    SERVER parts_prices_fdw
    OPTIONS (user 'admin', password 'Password123');

CREATE SCHEMA IF NOT EXISTS market;

-- read-FT: поиск site_id по имени (find-or-create).
CREATE FOREIGN TABLE market.sites (
    id         integer,
    name       text,
    url        text,
    note       text,
    created_at timestamptz
) SERVER parts_prices_fdw OPTIONS (schema_name 'market', table_name 'sites');

-- write-FT: INSERT нового сайта (id serial + created_at default — на стороне remote).
CREATE FOREIGN TABLE market.sites_w (
    name text,
    url  text,
    note text
) SERVER parts_prices_fdw OPTIONS (schema_name 'market', table_name 'sites');

-- write-FT: INSERT оффера (id IDENTITY ALWAYS + observed_at default — на стороне remote).
CREATE FOREIGN TABLE market.observations_w (
    smart_part_id text,
    site_id       integer,
    price         numeric,
    currency      text,
    url           text,
    note          text,
    created_by    text
) SERVER parts_prices_fdw OPTIONS (schema_name 'market', table_name 'observations');

COMMENT ON FOREIGN TABLE market.observations_w IS
    'Запись US-цен в parts_prices через FDW — в одной транзакции с smart-публикацией (save_to_smart).';

COMMIT;
