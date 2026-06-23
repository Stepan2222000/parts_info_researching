-- 008: новый терминальный статус task_run_status='skipped_smart_approved'.
-- Воркер закрывает им задачу, не запуская research, если артикул уже есть в smart.parts
-- с is_unverified=false (состав сверён человеком) или is_draft=false (запись финализирована).
-- ALTER TYPE ... ADD VALUE нельзя выполнять внутри транзакции — отдельным statement.
ALTER TYPE task_run_status ADD VALUE IF NOT EXISTS 'skipped_smart_approved';
