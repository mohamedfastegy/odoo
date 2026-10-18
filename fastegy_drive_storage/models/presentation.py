from odoo import models
from odoo.exceptions import UserError
from ..storage.presentation import INSTALLATION_MODELS, resolve_anchor, category, business_folders


class BusinessAttachmentFolders(models.Model):
    _inherit = "ir.attachment"

    def _fd_business_folders(self, storage):
        self.ensure_one()
        record = (self.env[self.res_model].sudo().browse(self.res_id).exists()
                  if self.res_model in self.env and self.res_id else False)
        anchor = resolve_anchor(record) if record else False
        company = self.company_id
        if anchor and "company_id" in anchor._fields and anchor.company_id:
            if company and company != anchor.company_id:
                raise ValueError("Attachment and business record belong to different companies")
            company = anchor.company_id
        model = anchor._name if anchor else (self.res_model or "unassigned")
        record_id = anchor.id if anchor else self.res_id
        if self.res_model in INSTALLATION_MODELS:
            app_key, app_label = "fastegy_installation", "التركيبات"
        else:
            lines = storage.fd_scope_line_ids.filtered(lambda line: line.current
                and line.model_name == self.res_model and line.status == "ready"
                and (line.kind == "model" or line.field_name == self.res_field))
            owners = {part.strip() for line in lines for part in (line.source_modules or "").split(",") if part.strip()}
            module = self.env["ir.module.module"].sudo().search([("name", "in", sorted(owners))], limit=2)
            if len(module) == 1:
                app_key, app_label = module.name, module.with_context(lang="ar_001").shortdesc
            else:
                app_key, app_label = "model:" + model, anchor._description if anchor else model
        if app_key == "fastegy_installation":
            app_label = "التركيبات"
        reference = False
        if anchor and anchor._rec_name and anchor._rec_name in anchor._fields:
            field = anchor._fields[anchor._rec_name]
            if field.type in ("char", "text"):
                reference = anchor[anchor._rec_name]
        if model == "fastegy.survey.response":
            reference = "معاينة — %s" % (reference or record_id)
        elif model in INSTALLATION_MODELS and model != "fastegy.installation":
            reference = "سجل مستقل — %s #%s" % (model, record_id)
        # Check the actual message link; do not treat every ordinary attachment as chatter.
        chatter = not self.res_field and bool(self.env["mail.message"].sudo().search_count([
            ("model", "=", self.res_model), ("res_id", "=", self.res_id),
            ("attachment_ids", "in", self.id)], limit=1))
        return business_folders(company.id, app_key, app_label, model, record_id,
            reference, category(self.res_model, self.mimetype, chatter))


class BusinessStorageFolders(models.Model):
    _inherit = "fs.storage"

    def action_fd_organize_existing(self):
        self._fd_admin()
        for storage in self:
            if storage.protocol != "fastegy_gdrive" or storage.fd_readonly or storage.fd_clone_readonly:
                raise UserError("تنظيم الملفات يتطلب اتصال درايف قابلًا للكتابة.")
            blobs = self.env["fastegy.drive.blob"].sudo().search([
                ("storage_id", "=", storage.id), ("state", "=", "active"), ("attachment_id", "!=", False)])
            for attachment in blobs.mapped("attachment_id").sorted("id"):
                if (attachment.store_fname or "").startswith(storage.code + "://"):
                    self.env["fastegy.drive.job"]._enqueue(storage, attachment, "metadata")
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": "تمت جدولة تنظيم الملفات الموجودة", "message": "يُغيّر التنظيم المجلدات فقط مع الاحتفاظ بمعرّفات الملفات وروابط أودو. تابع التنفيذ من طابور النقل.",
            "type": "success", "sticky": False}}
