-- 012: прогрессивная выдача результата по этапам (турнам) + профили этапов.
--
--   * task_runs.profile        — профиль этапов рана: {"preset": ..., "stages": [...]}.
--                                NULL = legacy-ран (до профилей, гнался полным пайплайном
--                                с phase2) — при reuse-проверках трактуется как full.
--   * task_runs.stage_outcomes — исход каждого этапа: "ok" | "pending" | "running" |
--                                "not_applicable" | "skipped_by_profile" | "failed: <текст>".
--                                Ошибки этапов не скрываем — best-effort провал виден тут.
--   * run_turns                — пер-турновые снапшоты: строка создаётся на СТАРТЕ этапа
--                                (running — прогресс виден сразу), закрывается ok+snapshot
--                                либо failed+error. Дельты /turns считаются из снапшотов.

BEGIN;

ALTER TABLE task_runs
    ADD COLUMN profile        JSONB,
    ADD COLUMN stage_outcomes JSONB;

COMMENT ON COLUMN task_runs.profile IS
    'Профиль этапов рана {"preset","stages"}; NULL = legacy (полный пайплайн, включая phase2).';
COMMENT ON COLUMN task_runs.stage_outcomes IS
    'Исход каждого этапа: ok/pending/running/not_applicable/skipped_by_profile/failed: <текст>.';

CREATE TABLE run_turns (
    id          BIGSERIAL PRIMARY KEY,
    run_id      BIGINT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    turn_idx    INT NOT NULL,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed')),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    error       TEXT,
    result_json JSONB,
    UNIQUE (run_id, turn_idx)
);

CREATE INDEX idx_run_turns_run ON run_turns(run_id, turn_idx);

COMMENT ON TABLE run_turns IS
    'Пер-турновые снапшоты StructuredResult: insert-at-start (running) -> update ok+snapshot/failed+error.';
COMMENT ON COLUMN run_turns.turn_idx IS 'Сквозной счётчик попыток этапов внутри рана (1..N).';
COMMENT ON COLUMN run_turns.stage IS
    'main | family_expansion | low_confidence | kit_contents | price_fallback | difference | phase2';
COMMENT ON COLUMN run_turns.result_json IS 'Снапшот StructuredResult после успешного турна (NULL у running/failed).';

COMMIT;
