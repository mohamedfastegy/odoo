"""Fixed native maintenance actions; no user-supplied Python is executed."""
from odoo import fields, models
from odoo.exceptions import AccessError, ValidationError


OPERATIONS = {
    "cron_drive_scope": ("fs.storage", "_cron_fd_scope"),
    "cron_drive_scope_jobs": ("fastegy.drive.job", "_cron_fd_scope_jobs"),
    "cron_drive_health": ("fs.storage", "_cron_health"),
    "cron_drive_jobs": ("fastegy.drive.job", "_cron_process"),
    "cron_drive_migration": ("fastegy.drive.migration", "_cron_run_batches"),
}


class DriveMaintenanceAction(models.Model):
    _inherit = "ir.actions.server"

    state = fields.Selection(selection_add=[("fastegy_drive_maintenance", "صيانة تخزين درايف")],
                             ondelete={"fastegy_drive_maintenance": "cascade"})

    def _run_action_fastegy_drive_maintenance_multi(self, eval_context=None):
        self.ensure_one()
        # أودو يرفع صلاحية قراءة تعريف الإجراء؛ التنفيذ يحتفظ بصلاحيات المستدعي الأصلي.
        env = (eval_context or {}).get("env", self.env)
        if not env.user.has_group("base.group_system"):
            raise AccessError("صيانة درايف متاحة لمسؤول الإعدادات فقط.")
        for xmlid, (model, method) in OPERATIONS.items():
            cron = env.ref("fastegy_drive_storage." + xmlid, raise_if_not_found=False)
            if cron and cron.ir_actions_server_id.id == self.id and self.model_id.model == model:
                # قائمة ثابتة من كود الإضافة؛ لا نقبل أسماء موديلات أو دوال من الحقول أو السياق.
                getattr(env[model], method)()
                return False
        raise ValidationError("هذا الإجراء غير مرتبط بمهمة درايف الأصلية.")
