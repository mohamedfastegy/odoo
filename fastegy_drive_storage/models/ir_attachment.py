import uuid
import hashlib
import base64
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from ..storage.policy import safe_name

from .fs_storage import PROTOCOL


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    @api.model_create_multi
    def create(self, vals_list):
        records = self.browse()
        for values in vals_list:
            values = dict(values)
            data = values.get("raw")
            if data is None and values.get("datas"):
                data = base64.b64decode(values["datas"])
            size = len(data) if data else values.get("file_size", 0)
            context = dict(self.env.context, fd_original_name=values.get("name"))
            if not context.get("storage_location"):
                selected = self.env["fastegy.drive.rule"]._choose(values, size, self._compute_mimetype(values))
                if selected:
                    context["storage_location"] = selected
            record = super(IrAttachment, self.with_context(context)).create([values])
            record._fd_sync_registry()
            records |= record.with_env(self.env)
        return records

    def _storage_write_option(self, fs):
        options = super()._storage_write_option(fs)
        if getattr(fs, "protocol", None) == PROTOCOL:
            options = dict(options, mimetype=self.env.context.get("mimetype") or self.env.context.get("fd_mimetype"))
        return options

    def _fd_adopt_verified_reference(self, storage, path, data):
        """Link verified existing bytes using the same internal ORM layer as Odoo's binary inverse.

        Normal attachment create/write deliberately discard store_fname/checksum/file_size.
        This private method is admin-only and never accepts a remote identity through RPC context.
        """
        self.ensure_one()
        storage.ensure_one()
        self.env["fs.storage"]._fd_admin()
        self.check_access("write")
        self.check("write")
        if self.store_fname or self.db_datas or storage.protocol != PROTOCOL:
            raise UserError(_("Reference adoption requires an empty attachment and a verified Drive connection."))
        if uuid.UUID(path).hex != path:
            raise UserError(_("Invalid reference identity."))
        client = storage._fd_client()
        if client.readonly:
            raise UserError(_("This Drive connection is read-only."))
        remote = client.lookup(path)
        if (not remote or not remote.get("shortcutDetails") or client.file_path(remote) != path
                or int(remote.get("size", -1)) != len(data)
                or remote.get("md5Checksum") != hashlib.md5(data).hexdigest()):
            raise UserError(_("The selected reference failed integrity verification."))
        from odoo.addons.base.models.ir_attachment import IrAttachment as NativeAttachment
        storage._fd_lock_lifecycle()
        super(NativeAttachment, self.sudo()).write({"store_fname": storage.code + "://" + path,
            "file_size": len(data), "checksum": self._compute_checksum(data), "db_datas": False,
            "index_content": False})
        self._fd_sync_registry()
        if self.with_context(bin_size=False).raw != data:
            raise UserError(_("Reference readback verification failed."))
        return True

    def _fd_sync_registry(self, force_metadata=False):
        for attachment in self:
            fname = attachment.store_fname or ""
            prior = self.env["fastegy.drive.blob"].sudo().search([("attachment_id", "=", attachment.id), ("state", "=", "active")])
            for prior_storage in prior.mapped("storage_id").sorted("id"):
                prior_storage._fd_lock_lifecycle()
            for prior_blob in prior:
                if not prior_blob._live_references():
                    prior_blob.write({"state": "orphan", "orphan_since": fields.Datetime.now()})
            if "://" not in fname:
                continue
            code, path = fname.split("://", 1)
            storage = self.env["fs.storage"].sudo().get_by_code(code)
            if not storage or storage.protocol != PROTOCOL:
                continue
            storage._fd_lock_lifecycle()
            blob_model = self.env["fastegy.drive.blob"].sudo()
            previous = blob_model.search([("attachment_id", "=", attachment.id), ("state", "=", "active"), ("path", "!=", path)])
            for previous_blob in previous:
                if not previous_blob._live_references():
                    previous_blob.write({"state": "orphan", "orphan_since": fields.Datetime.now()})
            blob = blob_model.search([("storage_id", "=", storage.id), ("path", "=", path)], limit=1)
            metadata = {}
            fs = storage._get_filesystem()
            if hasattr(fs, "client"):
                client = fs.client
                client.nested_layout = True
                metadata = client.lookup(path)
                if not metadata:
                    raise UserError(_("The uploaded file could not be found in Drive."))
                folders = []
                if storage.fd_folder_layout == "record":
                    company_id = attachment.company_id.id or self.env.company.id
                    folders = ["company_%s" % company_id, safe_name(attachment.res_model, "unassigned"),
                               "record_%s" % (attachment.res_id or 0), safe_name(attachment.res_field, "attachments")]
                elif storage.fd_folder_layout == "business":
                    folders = attachment._fd_business_folders(storage)
                # بيانات العرض لا تغيّر المفتاح؛ تغيير اسم سجل الأعمال لا يكسر المرفق.
                metadata = client.update_presentation(metadata, safe_name(attachment.name), attachment.mimetype or "application/octet-stream", folders)
                details = {"schema": 1, "path": path, "odoo": {"model": attachment.res_model, "id": attachment.res_id,
                    "field": attachment.res_field, "attachment": attachment.id, "sha1": attachment.checksum}}
                prior_details = json.loads(metadata.get("description") or "{}")
                if "reference" in prior_details:
                    details["reference"] = prior_details["reference"]
                client._json("PATCH", "https://www.googleapis.com/drive/v3/files/" + metadata["id"], retry_safe=True,
                    params={"fields": "id", "supportsAllDrives": "true"}, json={"description": json.dumps(details, ensure_ascii=False)})
            values = {"name": attachment.name or path, "attachment_id": attachment.id,
                "res_model": attachment.res_model, "res_id": attachment.res_id, "res_field": attachment.res_field,
                "mimetype": attachment.mimetype, "size": attachment.file_size, "checksum": attachment.checksum,
                "file_id": metadata.get("id") or blob.file_id, "md5": metadata.get("md5Checksum") or blob.md5,
                "state": "active", "orphan_since": False, "checked_at": fields.Datetime.now()}
            if blob:
                blob.write(values)
            else:
                blob_model.create(dict(values, storage_id=storage.id, path=path))
                storage._fd_event("upload", attachment, byte_count=attachment.file_size)
        return True

    def write(self, vals):
        if self and any(k in vals for k in ("raw", "datas", "name", "mimetype", "res_model", "res_id", "res_field")):
            for attachment in self:
                values = dict(vals)
                context = dict(self.env.context, fd_original_name=values.get("name", attachment.name))
                data = values.get("raw")
                if data is None and values.get("datas"):
                    data = base64.b64decode(values["datas"])
                scope = {"res_model": values.get("res_model", attachment.res_model),
                         "res_id": values.get("res_id", attachment.res_id), "res_field": values.get("res_field", attachment.res_field)}
                if not context.get("storage_location"):
                    selected = self.env["fastegy.drive.rule"]._choose(scope,
                        len(data) if data is not None else attachment.file_size, values.get("mimetype", attachment.mimetype))
                    if selected:
                        context["storage_location"] = selected
                        current = (attachment.store_fname or "").partition("://")[0]
                        if data is None and {"res_model", "res_id", "res_field"}.intersection(values) and current != selected:
                            values["raw"] = attachment.with_context(bin_size=False).raw
                attachment.with_context(context)._fd_write_existing(values)
                attachment._fd_sync_registry()
            return True
        return super().write(vals)

    def _fd_write_existing(self, vals):
        # Composer uploads can be local until mail.thread assigns their final
        # model/id. Updating ownership alone does not run OCA's binary routing.
        if (not self or self.env.context.get("storage_location")
                or {"raw", "datas"}.intersection(vals)
                or not {"res_model", "res_id", "res_field"}.intersection(vals)):
            return super().write(vals)

        with self.env.cr.savepoint():
            for attachment in self:
                res_model = vals.get("res_model", attachment.res_model)
                res_field = vals.get("res_field", attachment.res_field)
                storage_code = self.env["fs.storage"].with_context(
                    attachment_res_model=res_model,
                    attachment_res_field=res_field,
                ).get_default_storage_code_for_attachments()
                storage = self.env["fs.storage"].sudo().get_by_code(storage_code)
                if (not res_model or not storage or storage.protocol != PROTOCOL
                        or vals.get("type", attachment.type) != "binary"
                        or (attachment.store_fname or "").startswith(storage.code + "://")):
                    super(IrAttachment, attachment).write(vals)
                    continue

                attachment.check_access("read")
                attachment.check_access("write")
                attachment.check("read")
                attachment.check("write")
                content = attachment.with_context(bin_size=False).raw
                if not content:
                    super(IrAttachment, attachment).write(vals)
                    continue
                expected = hashlib.sha256(content).digest()
                target = attachment.with_context(storage_location=storage.code)
                super(IrAttachment, target).write(dict(vals, raw=content, index_content=False))
                if (not (attachment.store_fname or "").startswith(storage.code + "://")
                        or attachment.db_datas
                        or hashlib.sha256(attachment._file_read(attachment.store_fname)).digest() != expected):
                    raise UserError(_("Drive verification failed; the original attachment was preserved."))
        return True

    def _get_datas_related_values(self, data, mimetype):
        values = super(IrAttachment, self.with_context(fd_mimetype=mimetype))._get_datas_related_values(data, mimetype)
        code = self.env.context.get("storage_location") or self._storage()
        storage = self.env["fs.storage"].sudo().get_by_code(code)
        if storage and storage.protocol == PROTOCOL:
            # فهرسة النص الكامل قد تحتفظ بنسخة نصية كبيرة من الملف داخل قاعدة أودو.
            values["index_content"] = False
        return values

    def unlink(self):
        blobs = self.env["fastegy.drive.blob"].sudo().search([("attachment_id", "in", self.ids)])
        for storage in blobs.mapped("storage_id"):
            storage._fd_lock_lifecycle()
        result = super().unlink()
        for blob in blobs.exists():
            if not blob._live_references():
                blob.write({"attachment_id": False, "state": "orphan", "orphan_since": fields.Datetime.now()})
        return result

    @api.model
    def _get_fs_path(self, storage_code, bin_data):
        storage = self.env["fs.storage"].sudo().get_by_code(storage_code)
        if storage and storage.protocol == PROTOCOL:
            fixed = self.env.context.get("fd_job_fixed_path")
            if fixed and self.env.su:
                return uuid.UUID(str(fixed)).hex
            # كل كتابة لها مفتاح مستقل؛ التراجع عن معاملة لا يبدل محتوى ملف سابق.
            return uuid.uuid4().hex
        return super()._get_fs_path(storage_code, bin_data)

    @api.model
    def _storage_file_read(self, fname):
        code = fname.partition("://")[0]
        storage = self.env["fs.storage"].sudo().get_by_code(code)
        if storage and storage.protocol == PROTOCOL:
            # أخطاء درايف يجب أن تظهر؛ لا تُحوَّل إلى صورة فارغة بصمت.
            fs, _code, path = self._fs_parse_store_fname(fname)
            with fs.open(path, "rb") as stream:
                content = stream.read()
            references = self.sudo().search([("store_fname", "=", fname),
                "|", ("res_field", "=", False), ("res_field", "!=", False)])
            checksums = set(references.mapped("checksum")) - {False, ""}
            if checksums and (len(checksums) != 1 or hashlib.sha1(content).hexdigest() not in checksums):
                raise UserError(_("Remote file content changed unexpectedly; the original checksum no longer matches."))
            return content
        return super()._storage_file_read(fname)

