import secrets
import os
import re
import time
import uuid
from urllib.parse import urlencode, urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..storage.drive_client import GoogleDriveClient

PROTOCOL = "fastegy_gdrive"
CALLBACK = "/fastegy_drive_storage/oauth/callback"


class FsStorage(models.Model):
    _inherit = "fs.storage"

    @api.model
    def _get_protocols(self):
        protocols = super()._get_protocols()
        # fsspec.available_protocols() excludes classes registered at runtime.
        if not any(code == PROTOCOL for code, _label in protocols):
            protocols.append((PROTOCOL, "FastEgy Google Drive"))
        return protocols

    @api.model
    def _get_options_protocol(self):
        protocols = super()._get_options_protocol()
        if not any(code == PROTOCOL for code, _label in protocols):
            protocols.append((PROTOCOL, "FastEgy Google Drive"))
        return protocols

    fd_client_id = fields.Char(string="Google Client ID", groups="base.group_system", copy=False)
    fd_client_secret = fields.Char(string="Google Client Secret", groups="base.group_system", copy=False)
    fd_refresh_token = fields.Char(groups="base.group_system", copy=False)
    fd_root_folder_id = fields.Char(string="Drive folder ID", copy=False)
    fd_namespace = fields.Char(default=lambda self: str(uuid.uuid4()), readonly=True, copy=False)
    fd_bound_database = fields.Char(default=lambda self: self.env.cr.dbname, readonly=True, copy=False)
    fd_bound_origin = fields.Char(default=lambda self: self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/"), readonly=True, copy=False)
    fd_allow_writes = fields.Boolean(string="Allow new uploads", default=False)
    fd_readonly = fields.Boolean(string="Read only", default=True)
    fd_max_file_mb = fields.Integer(string="Trial file limit (MiB)", default=20)
    fd_connection_checked = fields.Boolean(readonly=True, copy=False)
    fd_redirect_uri = fields.Char(compute="_compute_fd_redirect_uri")
    fd_clone_readonly = fields.Boolean(compute="_compute_fd_clone_readonly")

    def _fd_admin(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only Settings administrators may manage Drive storage."))

    def _fd_store_pattern(self):
        self.ensure_one()
        # الشرطة السفلية محرف خاص في SQL LIKE وليست جزءًا حرفيًا من الاسم.
        return self.code.replace("_", "\\_") + "://%"

    @api.ondelete(at_uninstall=False)
    def _fd_no_delete_referenced_storage(self):
        for rec in self.filtered(lambda s: s.protocol == PROTOCOL):
            if self.env["ir.attachment"].sudo().search_count(
                [("store_fname", "=like", rec._fd_store_pattern()),
                 "|", ("res_field", "=", False), ("res_field", "!=", False)], limit=1
            ):
                raise UserError(_("Move or restore all attachments before deleting this storage."))

    @api.depends("protocol")
    def _compute_fd_redirect_uri(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        for rec in self:
            rec.fd_redirect_uri = base + CALLBACK

    @api.depends("fd_bound_database", "fd_bound_origin")
    def _compute_fd_clone_readonly(self):
        origin = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        for rec in self:
            rec.fd_clone_readonly = (rec.fd_bound_database != self.env.cr.dbname
                                     or rec.fd_bound_origin != origin
                                     or os.environ.get("FASTEGY_DRIVE_READONLY") == "1")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("protocol") == PROTOCOL:
                # منع تفعيل النقل أو تغيير التخزين العام بمجرد تثبيت الإضافة.
                vals.update(is_cacheable=False, use_filename_obfuscation=True,
                            directory_path=False, autovacuum_gc=False)
        return super().create(vals_list)

    def write(self, vals):
        protected = {"code", "protocol", "fd_namespace", "fd_root_folder_id", "fd_bound_database", "fd_bound_origin", "fd_client_id"}
        if protected.intersection(vals):
            for rec in self.filtered(lambda s: s.protocol == PROTOCOL):
                changes = any(vals[k] != rec[k] for k in protected.intersection(vals))
                if changes and self.env["ir.attachment"].sudo().search_count(
                    [("store_fname", "=like", rec._fd_store_pattern()),
                     "|", ("res_field", "=", False), ("res_field", "!=", False)], limit=1
                ):
                    raise ValidationError(_("A storage with files cannot change its identity or folder."))
        records = self.filtered(lambda s: s.protocol == PROTOCOL)
        if records and not self.env.context.get("fd_secret_rewrap") and {"fd_client_id", "fd_client_secret", "fd_refresh_token", "fd_root_folder_id"}.intersection(vals):
            vals = dict(vals, fd_connection_checked=False)
        return super().write(vals)

    @api.constrains("code", "protocol", "directory_path", "is_cacheable", "use_filename_obfuscation",
                    "fd_max_file_mb", "fd_root_folder_id", "autovacuum_gc",
                    "use_as_default_for_attachments", "fd_allow_writes", "fd_connection_checked")
    def _fd_check_configuration(self):
        for rec in self.filtered(lambda s: s.protocol == PROTOCOL):
            if rec.code in {"file", "db"} or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", rec.code or ""):
                raise ValidationError(_("Use a unique lowercase code such as fastegy_drive; file and db are reserved."))
            if rec.directory_path or rec.is_cacheable or not rec.use_filename_obfuscation:
                raise ValidationError(_("Drive trial requires an empty Directory, disabled cache, and filename obfuscation."))
            if rec.autovacuum_gc:
                raise ValidationError(_("Automatic remote garbage collection is disabled in the trial. Retain remote files for recovery."))
            if rec.use_as_default_for_attachments:
                raise ValidationError(_("Use model/field routing in this trial, not the global attachment default."))
            if rec.fd_allow_writes and (not rec.fd_connection_checked or rec.fd_clone_readonly):
                raise ValidationError(_("Test the connection in its original database before allowing uploads."))
            if not 1 <= rec.fd_max_file_mb <= 512:
                raise ValidationError(_("The file limit must be between 1 and 512 MiB. Test worker memory before increasing it."))
            if rec.fd_root_folder_id == "root":
                raise ValidationError(_("Use an explicit dedicated folder ID, not My Drive root."))

    def _get_fs_options(self):
        self.ensure_one()
        if self.protocol != PROTOCOL:
            return super()._get_fs_options()
        rec = self.sudo()
        if not all((rec.fd_client_id, rec.fd_client_secret, rec.fd_refresh_token, rec.fd_root_folder_id)):
            raise UserError(_("Complete the Drive folder and authorize the Google account first."))
        return {
            "client_id": rec.fd_client_id,
            "client_secret": rec.fd_client_secret,
            "refresh_token": rec.fd_refresh_token,
            "root_folder_id": rec.fd_root_folder_id,
            "namespace": rec.fd_namespace,
            "readonly": rec.fd_readonly or not rec.fd_allow_writes or rec.fd_clone_readonly,
            "max_file_bytes": rec.fd_max_file_mb * 1024 * 1024,
            "timeout": 20,
            "skip_instance_cache": True,
        }

    def _fd_client(self):
        options = self._get_fs_options().copy()
        options.pop("skip_instance_cache", None)
        return GoogleDriveClient(**options)

    def _check_connection(self, fs, check_connection_method):
        if self.protocol == PROTOCOL:
            # فحص أودو الافتراضي ينشئ ملف علامة؛ ذلك يمنع القراءة من مخزن للقراءة فقط.
            fs.validate_root()
            return True
        return super()._check_connection(fs, check_connection_method)

    def action_fd_authorize(self):
        self._fd_admin()
        self.ensure_one()
        if self.protocol != PROTOCOL or not self.fd_client_id or not self.fd_client_secret:
            raise UserError(_("Save Google Client ID and Secret on a Drive storage first."))
        if self.fd_clone_readonly:
            raise UserError(_("Create a separate Drive storage for this copied database."))
        if urlsplit(self.fd_redirect_uri).scheme != "https":
            raise UserError(_("Configure an HTTPS web.base.url before connecting Google."))
        from odoo.http import request

        state = secrets.token_urlsafe(32)
        request.session["fastegy_drive_oauth"] = {
            "state": state, "storage_id": self.id,
            "uid": self.env.uid, "expires": time.time() + 600,
            "redirect_uri": self.fd_redirect_uri,
        }
        return {"type": "ir.actions.act_url", "target": "self", "url":
            "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
                "client_id": self.fd_client_id,
                "redirect_uri": self.fd_redirect_uri,
                "response_type": "code", "scope": "https://www.googleapis.com/auth/drive.file",
                "access_type": "offline", "prompt": "consent", "state": state,
            })}

    def action_fd_test_connection(self):
        self._fd_admin()
        self.ensure_one()
        client = self._fd_client()
        # فحص المجلد فقط؛ لا ينقل ملفات النظام ولا ينشئ ملفات اختبار.
        folder = client.check_connection()
        self.fd_connection_checked = True
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("Drive connection succeeded"),
            "message": _("Dedicated folder is accessible: %s") % folder.get("name", self.fd_root_folder_id),
            "type": "success", "sticky": False,
        }}

    def action_fd_disconnect(self):
        self._fd_admin()
        self.ensure_one()
        if self.env["ir.attachment"].sudo().search_count(
            [("store_fname", "=like", self._fd_store_pattern()),
             "|", ("res_field", "=", False), ("res_field", "!=", False)], limit=1
        ):
            raise UserError(_("Files still use this connection. Keep credentials until they are restored or moved."))
        self.write({"fd_refresh_token": False, "fd_connection_checked": False,
                    "fd_allow_writes": False, "fd_readonly": True})

