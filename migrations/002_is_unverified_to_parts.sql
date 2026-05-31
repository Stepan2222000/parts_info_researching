-- ============================================================================
-- 002 — перенос is_unverified с part_components (per-link) на parts (per-part)
-- ============================================================================
-- Применять на БД Smart (и smart_test, и прод smart). Идемпотентно: повторный
-- прогон не падает. После применения — пересинхронить FDW в parts_research
-- (DROP FOREIGN TABLE smart.parts, smart.part_components CASCADE; IMPORT FOREIGN
-- SCHEMA …, см. 001_init.sql), иначе foreign-таблицы останутся рассинхронены.
--
-- Семантика: is_unverified=true → состав НЕ сверен человеком; false → сверен и
-- заморожен. Раньше флаг жил на каждой связке part_components; теперь — один
-- флаг на запчасть-набор (parts). Бэкфилл: набор «сверен» (false) только если
-- ВСЕ его связки были сверены (bool_or по родителю); не-наборы → true (дефолт).
--
-- Проверено прототипом на smart_test (в транзакции с ROLLBACK):
--   parts.is_unverified → 39 false / 838 true; part_components без is_unverified;
--   view пересоздан; триггер заморозки блокирует правку состава сверенных наборов.
-- ============================================================================

BEGIN;

-- View зависит от part_components.is_unverified → снимаем перед DROP COLUMN.
DROP VIEW IF EXISTS parts_with_components;

-- 1. Новый per-part флаг (default true = не сверено).
ALTER TABLE parts ADD COLUMN IF NOT EXISTS is_unverified boolean NOT NULL DEFAULT true;

-- 2. Бэкфилл из старого per-link флага + удаление старой колонки.
--    Guard по существованию колонки делает шаг идемпотентным (повторный прогон
--    пропустит этот блок, т.к. колонки уже нет).
DO $migrate$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'part_components'
          AND column_name = 'is_unverified'
    ) THEN
        UPDATE parts p
        SET is_unverified = sub.iu
        FROM (
            SELECT parent_id, bool_or(is_unverified) AS iu
            FROM part_components
            GROUP BY parent_id
        ) sub
        WHERE p.id = sub.parent_id;

        ALTER TABLE part_components DROP COLUMN is_unverified;
    END IF;
END
$migrate$;

-- 3. Пересоздаём view: флаг is_unverified теперь на уровне part (а не в составе).
CREATE VIEW parts_with_components AS
SELECT
    p.id,
    p.name,
    p.articles,
    (SELECT array_agg(pb.brand ORDER BY pb.brand)
       FROM part_brands pb WHERE pb.part_id = p.id) AS brands,
    p.model,
    p.product_type,
    p.weight_kg,
    p.is_draft,
    p.description,
    p.is_unverified,
    (SELECT array_agg(jsonb_build_object(
                'child_id', pc.child_id,
                'quantity', pc.quantity,
                'can_be_sold_separately', pc.can_be_sold_separately
            ) ORDER BY pc.child_id)
       FROM part_components pc WHERE pc.parent_id = p.id) AS components
FROM parts p;

-- 4. Триггер заморозки состава по parts.is_unverified (эталон 3c49a037).
--    Состав набора заморожен, если parent сверен (is_unverified=false):
--    блокируем INSERT/UPDATE связок. DELETE не трогаем (каскады / разбор набора).
CREATE OR REPLACE FUNCTION check_part_components_kit_freeze()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'public', 'pg_catalog'
AS $kit_freeze$
DECLARE
    is_verified boolean;
BEGIN
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT NOT p.is_unverified INTO is_verified
        FROM parts p WHERE p.id = NEW.parent_id;
        IF is_verified THEN
            RAISE EXCEPTION
                'kit % composition is frozen (parts.is_unverified=false); '
                'set is_unverified=true to edit components', NEW.parent_id;
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$kit_freeze$;

DROP TRIGGER IF EXISTS part_components_kit_freeze ON part_components;
CREATE TRIGGER part_components_kit_freeze
    BEFORE INSERT OR UPDATE OR DELETE ON part_components
    FOR EACH ROW EXECUTE FUNCTION check_part_components_kit_freeze();

COMMIT;
