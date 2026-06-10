-- ============================================================================
-- 003 — синхронизация FDW со smart после миграций 014-015 (vehicle classes).
--
-- Контекст (central_smart_logic, 2026-06-10):
--   • в smart появились классы техники: parts.vehicle_classes TEXT[]
--     (источник правды) + справочник vehicle_classes + реестр
--     part_vehicle_classes (is_unverified, freeze);
--   • колонка parts.product_type УДАЛЕНА — тип вычисляется во VIEW
--     parts_with_components (тип класса с минимальной position).
--
-- Применено на живой базе parts_research 2026-06-10. Параллельные правки кода:
--   • curator/tools.py: _insert_part больше не пишет product_type;
--   • research/context.py + prompts.py: smart-плагин показывает
--     vehicle_classes вместо product_type.
-- Контракт ресёрча (StructuredResult.product_type, draft_parts.product_type)
-- пока не тронут — это заход 2 (агент определяет классы).
-- ============================================================================

BEGIN;

-- новые таблицы классов из smart
IMPORT FOREIGN SCHEMA public LIMIT TO (vehicle_classes, part_vehicle_classes)
    FROM SERVER smart_fdw INTO smart;

-- FT smart.parts: колонки product_type больше нет, есть vehicle_classes
ALTER FOREIGN TABLE smart.parts DROP COLUMN product_type;
ALTER FOREIGN TABLE smart.parts ADD COLUMN vehicle_classes text[];

-- КРИТИЧНО для INSERT через FDW: дефолт remote-таблицы не срабатывает
-- (postgres_fdw шлёт id=NULL явно) — нужен локальный DEFAULT с тем же
-- выражением, что generate_smart_id() в smart. До этого фикса save_to_smart
-- не мог вставлять детали вовсе.
ALTER FOREIGN TABLE smart.parts
    ALTER COLUMN id SET DEFAULT 'smart_' || lpad(floor(random() * 100000000)::int::text, 8, '0');

COMMIT;
