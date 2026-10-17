-- نسخ الاختبار تقرأ ملفات المصدر عند توفر الاعتماد، لكنها لا تكتب أو تشارك تلقائيًا.
UPDATE fs_storage SET fd_allow_writes = false, fd_readonly = true
WHERE protocol = 'fastegy_gdrive';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'fs_storage' AND column_name = 'fd_jobs_enabled') THEN
        UPDATE fs_storage SET fd_jobs_enabled = false, fd_monitor_enabled = false
        WHERE protocol = 'fastegy_gdrive';
    END IF;
    IF to_regclass('public.fastegy_drive_share') IS NOT NULL THEN
        UPDATE fastegy_drive_share SET active = false;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'fs_storage' AND column_name = 'fd_scope_active') THEN
        UPDATE fs_storage SET fd_scope_active = false, fd_scope_backfill_applied = false
        WHERE protocol = 'fastegy_gdrive';
    END IF;
END;
$$;
