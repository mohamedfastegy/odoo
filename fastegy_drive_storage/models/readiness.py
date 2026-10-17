"""Read-only operational evidence; discovery counts are never transfer counts."""
from odoo import fields, models


class DriveReadiness(models.Model):
    _inherit = "fs.storage"

    fd_transfer_status = fields.Text(string="حالة نقل القديم", compute="_compute_fd_transfer_status")
    fd_managed_mb = fields.Float(string="حجم ملفات أودو المدارة بالميجابايت",
                                compute="_compute_fd_transfer_status", digits=(16, 0))

    def _compute_fd_transfer_status(self):
        # القراءة لا تنشئ مهام ولا تتصل بدرايف؛ لا نحمّل محتوى الملفات لحساب المؤشرات.
        Migration = self.env["fastegy.drive.migration"].sudo()
        Job = self.env["fastegy.drive.job"].sudo()
        scanner = self.env.ref("fastegy_drive_storage.cron_drive_scope", raise_if_not_found=False)
        worker = self.env.ref("fastegy_drive_storage.cron_drive_scope_jobs", raise_if_not_found=False)
        for storage in self:
            storage.fd_managed_mb = int(storage.fd_blob_bytes / 1_000_000 + 0.5)
            if not storage.fd_scope_active or not storage.fd_scope_backfill_applied:
                storage.fd_transfer_status = "نقل القديم غير مفعّل ضمن النطاق الحالي."
                continue
            migrations = Migration.search([
                ("fd_scope_storage_id", "=", storage.id),
                ("fd_scope_plan_hash", "=", storage._fd_scope_generation()),
            ])
            groups = Job._read_group([("migration_id", "in", migrations.ids)],
                                     ["state"], ["__count", "byte_count:sum"])
            counts = {state: count for state, count, size in groups}
            verified = sum(size for state, count, size in groups if state == "done")
            lines = [
                "مرشحو المعاينة الحالية: %s ملف؛ هذا ليس عدد الملفات المنقولة." % sum(migrations.mapped("preview_count")),
                "مهام النقل: مكتمل %s؛ انتظار %s؛ إعادة محاولة %s؛ فشل %s؛ ملغى %s." % tuple(
                    counts.get(state, 0) for state in ("done", "pending", "retry", "failed", "cancelled")),
                "تم التحقق من نقل %s ميجابايت؛ لا يثبت ذلك تحرير نفس المساحة على القرص." % int(verified / 1_000_000 + 0.5),
                "متجاوزة أثناء التوجيه: %s؛ راجع التغطية والاستثناءات لمعرفة المسارات غير المدعومة." % sum(migrations.mapped("fd_scope_skipped")),
            ]
            if not migrations:
                lines.append("لا توجد معاينات في الخطة الحالية؛ لا يعني ذلك اكتمال النقل. افحص التغطية وحالة آخر فحص.")
            elif not groups and any(m.state in ("ready", "running") for m in migrations):
                lines.append("المعاينات جاهزة، لكن لم تُضف ملفاتها إلى الطابور بعد.")
            for label, cron in (("إعداد الطابور", scanner), ("تنفيذ النقل", worker)):
                when = fields.Datetime.to_string(fields.Datetime.context_timestamp(storage, cron.nextcall)) if cron and cron.nextcall else "غير محدد"
                lines.append("%s: %s؛ الموعد المقرر بتوقيت المستخدم: %s. الموعد لا يثبت أن المجدول يعمل." % (
                    label, "مفعّل" if cron and cron.active else "متوقف", when))
            if storage.fd_scope_error:
                lines.append("آخر مشكلة: " + storage.fd_scope_error)
            storage.fd_transfer_status = "\n".join(lines)
