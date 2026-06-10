-- ============================================================================
-- 004 — классы техники в драфтах: draft_parts.vehicle_classes.
--
-- Контекст: контракт research-агента переведён с product_type на
-- vehicle_classes (слаги из smart.vehicle_classes: boat, jetski, quad,
-- snowmobile, motorcycle, auto; кросс-типовый мультикласс легален).
--
-- draft_parts.product_type ОСТАЁТСЯ, но меняет смысл: агент его больше
-- не ресёрчит — колонка заполняется деривацией из классов (тип класса
-- с минимальной position) и нужна только кураторской морде.
-- ============================================================================

BEGIN;

-- FT импортирована (003) до переименования колонки в smart (avito_product_type
-- -> product_type, миграция 015) — синхронизируем имя. IMPORT FOREIGN SCHEMA
-- фиксирует remote-имя в options колонки, поэтому правим и его.
ALTER FOREIGN TABLE smart.vehicle_classes RENAME COLUMN avito_product_type TO product_type;
ALTER FOREIGN TABLE smart.vehicle_classes
    ALTER COLUMN product_type OPTIONS (SET column_name 'product_type');

ALTER TABLE draft_parts ADD COLUMN vehicle_classes TEXT[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN draft_parts.vehicle_classes IS
    'Классы техники от research-агента (слаги smart.vehicle_classes). '
    'Кросс-типовый мультикласс разрешён. Пусто => needs_human_review.';
COMMENT ON COLUMN draft_parts.product_type IS
    'Деривация из vehicle_classes (тип класса с min position) — только для '
    'кураторской морды. Агентом не ресёрчится, в smart не пишется.';

COMMIT;
