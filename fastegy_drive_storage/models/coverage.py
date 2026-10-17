import json

from odoo import _, fields, models


class DriveCoverage(models.Model):
    _name = "fastegy.drive.coverage"
    _description = "Installed model file storage coverage audit"
    _order = "id desc"

    name = fields.Char(default="File coverage audit", required=True)
    model_ids = fields.Many2many("ir.model", string="Models; empty audits all installed models")
    scanned_at = fields.Datetime(readonly=True)
    line_ids = fields.One2many("fastegy.drive.coverage.line", "audit_id", readonly=True)
    attachment_fields = fields.Integer(readonly=True)
    direct_fields = fields.Integer(readonly=True)
    computed_fields = fields.Integer(readonly=True)
    explanation = fields.Text(readonly=True)

    def action_scan(self):
        self.env["fs.storage"]._fd_admin()
        for audit in self:
            audit.line_ids.unlink()
            model_names = audit.model_ids.mapped("model") or sorted(self.env.registry.models)
            rows, attached, direct, computed = [], 0, 0, 0
            storage_model = self.env["fs.storage"]
            for name in model_names:
                if name not in self.env:
                    continue
                model = self.env[name]
                for field_name, field in sorted(model._fields.items()):
                    if field.type != "binary":
                        continue
                    is_stored = bool(field.store)
                    is_attachment = bool(field.attachment and is_stored)
                    code = storage_model.with_context(attachment_res_model=name,
                        attachment_res_field=field_name).get_default_storage_code_for_attachments() if is_attachment else False
                    storage = storage_model.sudo().get_by_code(code) if code else False
                    is_drive = bool(storage and storage.protocol == "fastegy_gdrive")
                    status = "computed" if not is_stored else ("drive" if is_attachment and is_drive else ("attachment_local" if is_attachment else "column"))
                    attached += int(is_attachment)
                    direct += int(is_stored and not is_attachment)
                    computed += int(not is_stored)
                    rows.append({"audit_id": audit.id, "model_name": name, "field_name": field_name,
                        "label": str(field.string), "status": status, "storage_code": code or False})
            self.env["fastegy.drive.coverage.line"].create(rows)
            audit.write({"scanned_at": fields.Datetime.now(), "attachment_fields": attached,
                "direct_fields": direct, "computed_fields": computed,
                "explanation": _("This inventory inspects actual installed binary/image field definitions. Direct database columns need a specific adapter or model change. Computed fields, embedded HTML data and custom filesystem/controller writes require workflow/source review. Routing rules can alter the destination by record; a model/field default is not proof that every record is covered. No data or field definition was converted.")})
        return True

    def action_export(self):
        self.ensure_one()
        self.env["fs.storage"]._fd_admin()
        return {"type": "ir.actions.act_url", "url": "/fastegy_drive_storage/coverage/%s" % self.id, "target": "new"}


class DriveCoverageLine(models.Model):
    _name = "fastegy.drive.coverage.line"
    _description = "Storage coverage field"
    _order = "status, model_name, field_name"

    audit_id = fields.Many2one("fastegy.drive.coverage", required=True, ondelete="cascade")
    model_name = fields.Char(required=True)
    field_name = fields.Char(required=True)
    label = fields.Char()
    status = fields.Selection([("drive", "Drive default; verify record rules"),
        ("attachment_local", "Attachment; Drive not the default"), ("column", "Direct DB column; adapter needed"),
        ("computed", "Computed/nonstored; inspect source")], required=True)
    storage_code = fields.Char()
