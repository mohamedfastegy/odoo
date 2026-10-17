from odoo import api, SUPERUSER_ID
from odoo.addons.fastegy_drive_storage.models.scheduler import OPERATIONS


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for xmlid, (model, method) in OPERATIONS.items():
        cron = env.ref("fastegy_drive_storage." + xmlid, raise_if_not_found=False)
        # لا نستبدل إجراء عدله المسؤول، ولا نغيّر التفعيل أو الموعد أو مستخدم المجدول.
        if cron and cron.model_id.model == model and cron.state == "code" and (cron.code or "").strip() == "model.%s()" % method:
            cron.write({"state": "fastegy_drive_maintenance"})
