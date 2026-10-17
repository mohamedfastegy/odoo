import hashlib
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class DriveImport(models.TransientModel):
    _name = "fastegy.drive.import"
    _description = "Select an authorized existing Drive file"

    storage_id = fields.Many2one("fs.storage", required=True, domain=[("protocol", "=", "fastegy_gdrive")])
    drive_file_id = fields.Char()
    model_id = fields.Many2one("ir.model", required=True, ondelete="cascade")
    record_id = fields.Integer(required=True)
    name = fields.Char(help="Optional filename override")
    attachment_id = fields.Many2one("ir.attachment", readonly=True)
    mode = fields.Selection([("reference", "Reference existing bytes via private shortcut"),
                             ("copy", "Independent managed copy")], default="reference", required=True)

    def action_picker(self):
        self.env["fs.storage"]._fd_admin()
        self.ensure_one()
        if not self.storage_id.fd_picker_api_key or not self.storage_id.fd_picker_app_id:
            raise UserError(_("Configure the restricted Google Picker API key and project number on this connection, or enter an already authorized file ID."))
        return {"type": "ir.actions.act_url", "url": "/fastegy_drive_storage/picker/%s" % self.id, "target": "new"}

    def action_import(self):
        self.env["fs.storage"]._fd_admin()
        self.ensure_one()
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.drive_file_id or ""):
            raise ValidationError(_("Enter a valid Drive file identifier."))
        target = self.env[self.model_id.model].browse(self.record_id).exists()
        if not target:
            raise UserError(_("Select an existing destination record."))
        target.check_access("write")
        client = self.storage_id._fd_client()
        if client.readonly:
            raise UserError(_("Enable verified uploads before importing."))
        metadata = client.metadata(self.drive_file_id)
        if metadata.get("trashed") or metadata.get("mimeType", "").startswith("application/vnd.google-apps."):
            raise UserError(_("Choose a binary file. Export native Google documents before importing them into attachment storage."))
        size = int(metadata.get("size", -1))
        if not 0 <= size <= client.max_file_bytes:
            raise UserError(_("The file exceeds this connection's configured limit."))
        response = client._request("GET", "https://www.googleapis.com/drive/v3/files/" + self.drive_file_id,
            retry_safe=True, params={"alt": "media", "supportsAllDrives": "true"}, stream=True)
        data = bytearray()
        try:
            client._check_status(response)
            for chunk in response.iter_content(262144):
                if len(data) + len(chunk) > size:
                    raise UserError(_("Drive returned more data than declared."))
                data.extend(chunk)
        finally:
            response.close()
        if len(data) != size or hashlib.md5(data).hexdigest() != metadata.get("md5Checksum"):
            raise UserError(_("The selected file failed integrity verification."))
        values = {"name": self.name or metadata["name"], "mimetype": metadata.get("mimeType"),
                  "res_model": target._name, "res_id": target.id, "public": False}
        if self.mode == "reference":
            path = uuid.uuid4().hex
            client.create_reference(path, metadata)
        else:
            values["raw"] = bytes(data)
        attachment = self.env["ir.attachment"].with_context(storage_location=self.storage_id.code).create(values)
        if self.mode == "reference":
            attachment._fd_adopt_verified_reference(self.storage_id, path, bytes(data))
        self.attachment_id = attachment
        self.storage_id._fd_event("import_existing", attachment, byte_count=size)
        return {"type": "ir.actions.act_window", "res_model": target._name, "res_id": target.id, "view_mode": "form"}


class DriveShare(models.Model):
    _name = "fastegy.drive.share"
    _description = "Revocable private portal attachment access"
    _order = "id desc"

    name = fields.Char(related="attachment_id.name", readonly=True)
    attachment_id = fields.Many2one("ir.attachment", required=True, ondelete="cascade")
    partner_id = fields.Many2one("res.partner", required=True, string="Exact recipient contact")
    expires_at = fields.Datetime(default=lambda self: fields.Datetime.now() + timedelta(days=7), required=True)
    active = fields.Boolean(default=True)
    url = fields.Char(compute="_compute_url")

    @api.depends("attachment_id")
    def _compute_url(self):
        for share in self:
            share.url = "/my/fastegy-drive/%s" % share.id if share.id else False

    @api.constrains("attachment_id", "partner_id", "expires_at")
    def _check_share(self):
        for share in self:
            share.attachment_id.check_access("read")
            share.attachment_id.check("read")
            if share.attachment_id.type != "binary" or share.attachment_id.public:
                raise ValidationError(_("Select a private binary attachment."))
            code, separator, _path = (share.attachment_id.store_fname or "").partition("://")
            storage = self.env["fs.storage"].sudo().get_by_code(code) if separator else False
            if not storage or storage.protocol != "fastegy_gdrive":
                raise ValidationError(_("Select an attachment stored in this Drive backend."))
            if share.expires_at <= fields.Datetime.now():
                raise ValidationError(_("Set a future expiry date."))
            if not self.env["res.users"].sudo().search_count([("partner_id", "=", share.partner_id.id), ("active", "=", True)], limit=1):
                raise ValidationError(_("The exact recipient contact must have an active Odoo login."))

    def action_revoke(self):
        self.env["fs.storage"]._fd_admin()
        self.write({"active": False})
