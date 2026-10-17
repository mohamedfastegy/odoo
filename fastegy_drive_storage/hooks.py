from odoo import _
from odoo.exceptions import UserError


def uninstall_hook(env):
    # حذف حقول التفويض أثناء وجود ملفات بعيدة يجعل المرفقات غير قابلة للقراءة.
    env["ir.attachment"].flush_model(["store_fname"])
    env["fs.storage"].flush_model(["code", "protocol"])
    env.cr.execute("""
        SELECT 1 FROM ir_attachment a
        JOIN fs_storage s ON left(a.store_fname, length(s.code) + 3) = s.code || '://'
        WHERE s.protocol = %s LIMIT 1
    """, ("fastegy_gdrive",))
    if env.cr.fetchone():
        raise UserError(_("Restore or move all Drive attachments before uninstalling FastEgy Drive Storage."))
