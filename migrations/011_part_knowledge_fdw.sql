-- 011: FDW-мост parts_research -> part_knowledge (база знаний по запчастям).
-- Куратор при публикации пишет факты-нюансы в knowledge.knowledge_facts В ТОЙ ЖЕ
-- транзакции, что и smart-публикация part'а (упали факты -> откатился part).
-- Идемпотентно: мост мог быть создан пробой заранее.

BEGIN;

CREATE EXTENSION IF NOT EXISTS postgres_fdw;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_foreign_server WHERE srvname = 'part_knowledge_fdw') THEN
    CREATE SERVER part_knowledge_fdw FOREIGN DATA WRAPPER postgres_fdw
      OPTIONS (host 'part_knowledge', port '5432', dbname 'part_knowledge', connect_timeout '5');
    CREATE USER MAPPING FOR admin SERVER part_knowledge_fdw
      OPTIONS (user 'admin', password 'Password123');
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS knowledge;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.foreign_tables
                 WHERE foreign_table_schema = 'knowledge'
                   AND foreign_table_name = 'knowledge_facts') THEN
    IMPORT FOREIGN SCHEMA knowledge LIMIT TO (knowledge_facts)
      FROM SERVER part_knowledge_fdw INTO knowledge;
  END IF;
END $$;

-- id (identity) / created_at / updated_at заполняет сама part_knowledge:
-- убираем их из ЛОКАЛЬНОГО описания foreign-таблицы, иначе FDW передаёт значение
-- и remote отвергает вставку (GENERATED ALWAYS) или получает NULL вместо default.
ALTER FOREIGN TABLE knowledge.knowledge_facts
  DROP COLUMN IF EXISTS id,
  DROP COLUMN IF EXISTS created_at,
  DROP COLUMN IF EXISTS updated_at;

COMMIT;
