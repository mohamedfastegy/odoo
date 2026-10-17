import hashlib
import json
import uuid
from datetime import timedelta

from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, ValidationError

from ..storage.policy import classify_error, safe_name


class DriveEvent(models.Model):
    _name = "fastegy.drive.event"
    _description = "Drive operation audit"
    _order = "id desc"

    storage_id = fields.Many2one("fs.storage", required=True, ondelete="cascade", index=True)
    attachment_id = fields.Many2one("ir.attachment", ondelete="set null")
    operation = fields.Char(required=True, index=True)
    outcome = fields.Selection([("ok", "Success"), ("error", "Failed"), ("warning", "Warning")], required=True)
    category = fields.Char(index=True)
    byte_count = fields.Float()
    actor_id = fields.Many2one("res.users", ondelete="set null")

    @api.model
    def _record(self, storage, operation, attachment=None, outcome="ok", category=False, byte_count=0):
        return self.sudo().create({"storage_id": storage.id, "operation": operation,
            "attachment_id": attachment.id if attachment else False, "outcome": outcome,
            "category": category, "byte_count": byte_count, "actor_id": self.env.uid})


class DriveBlob(models.Model):
    _name = "fastegy.drive.blob"
    _description = "Drive file registry and recovery manifest"
    _order = "id desc"
    _rec_name = "name"
    _sql_constraints = [("storage_path_unique", "unique(storage_id, path)", "A Drive path already has a registry entry.")]

    name = fields.Char(required=True)
    storage_id = fields.Many2one("fs.storage", required=True, ondelete="restrict", index=True)
    path = fields.Char(required=True, index=True)
    file_id = fields.Char(index=True)
    attachment_id = fields.Many2one("ir.attachment", ondelete="set null", index=True)
    res_model = fields.Char(index=True)
    res_id = fields.Integer(index=True)
    res_field = fields.Char()
    mimetype = fields.Char()
    size = fields.Float()
    checksum = fields.Char()
    md5 = fields.Char()
    state = fields.Selection([("active", "Referenced"), ("orphan", "Unreferenced"),
        ("missing", "Missing"), ("trashed", "In Drive trash")], default="active", required=True, index=True)
    checked_at = fields.Datetime()
    orphan_since = fields.Datetime()

    def _live_references(self):
        self.ensure_one()
        return self.env["ir.attachment"].sudo().search([
            ("store_fname", "=", self.storage_id.code + "://" + self.path),
            "|", ("res_field", "=", False), ("res_field", "!=", False)])

    def action_reconcile(self):
        self.env["fs.storage"]._fd_admin()
        for blob in self:
            client = blob.storage_id._fd_client()
            refs = blob._live_references()
            try:
                remote = client.metadata(blob.file_id) if blob.file_id else client.lookup(blob.path)
                if not remote:
                    raise FileNotFoundError()
                client.file_path(dict(remote, trashed=False))
                state = "trashed" if remote.get("trashed") else ("active" if refs else "orphan")
                if refs and (int(remote.get("size", -1)) != int(blob.size) or (blob.md5 and remote.get("md5Checksum") != blob.md5)):
                    blob.storage_id._fd_event("reconcile", outcome="error", category="integrity")
                    continue
                blob.write({"state": state, "file_id": remote["id"], "checked_at": fields.Datetime.now(),
                    "orphan_since": False if refs else (blob.orphan_since or fields.Datetime.now())})
                if refs:
                    source = refs[0]
                    blob.write({"attachment_id": source.id, "name": source.name, "res_model": source.res_model,
                        "res_id": source.res_id, "res_field": source.res_field, "mimetype": source.mimetype,
                        "checksum": source.checksum})
            except FileNotFoundError:
                blob.write({"state": "missing", "checked_at": fields.Datetime.now()})
                blob.storage_id._fd_event("reconcile", outcome="error", category="missing")
        return True

    def action_trash(self):
        self.env["fs.storage"]._fd_admin()
        for blob in self:
            # القفل يمنع أن تكتب نفس إضافة التخزين مرجعًا جديدًا أثناء المراجعة.
            blob.storage_id._fd_lock_lifecycle()
            if blob.storage_id.fd_clone_readonly or blob._live_references():
                raise UserError(_("A live or cloned database reference prevents trashing this file."))
            cutoff = fields.Datetime.now() - timedelta(days=blob.storage_id.fd_retention_days)
            if not blob.orphan_since or blob.orphan_since > cutoff:
                raise UserError(_("Reconcile first and wait until the retention period has elapsed."))
            client = blob.storage_id._fd_client()
            metadata = client.metadata(blob.file_id)
            client.trash(metadata)
            blob.state = "trashed"
            blob.storage_id._fd_event("trash", category="recoverable")
        return True

    def action_restore(self):
        self.env["fs.storage"]._fd_admin()
        for blob in self:
            blob.storage_id._fd_client().restore(blob.file_id)
            blob.write({"state": "active" if blob._live_references() else "orphan"})
            blob.storage_id._fd_event("restore")
        return True

    def action_restore_version(self):
        self.env["fs.storage"]._fd_admin()
        self.ensure_one()
        attachment = self.attachment_id.exists()
        if not attachment or self.state in ("missing", "trashed"):
            raise UserError(_("Select an available version whose original attachment still exists."))
        attachment.check_access("write")
        attachment.check("write")
        client = self.storage_id._fd_client()
        data = client.download(client.metadata(self.file_id))
        if self.checksum and hashlib.sha1(data).hexdigest() != self.checksum:
            raise UserError(_("Version checksum mismatch."))
        attachment.with_context(storage_location=self.storage_id.code).write({"raw": data,
            "name": self.name, "mimetype": self.mimetype})
        self.storage_id._fd_event("restore_version", attachment)
        return True

    def action_download(self):
        self.ensure_one()
        attachment = self.attachment_id.exists()
        if not attachment:
            raise UserError(_("This is an unlinked recovery entry. Restore or inspect it first."))
        attachment.check_access("read")
        attachment.check("read")
        return {"type": "ir.actions.act_url", "url": "/fastegy_drive_storage/blob/%s" % self.id, "target": "new"}

    def action_open_record(self):
        self.ensure_one()
        if self.res_model not in self.env:
            raise UserError(_("The original model is not installed."))
        record = self.env[self.res_model].browse(self.res_id).exists()
        record.check_access("read")
        return {"type": "ir.actions.act_window", "res_model": self.res_model,
            "res_id": record.id, "view_mode": "form"}


class DriveJob(models.Model):
    _name = "fastegy.drive.job"
    _description = "Durable independent Drive task"
    _order = "next_attempt, id"

    storage_id = fields.Many2one("fs.storage", required=True, ondelete="restrict", index=True)
    attachment_id = fields.Many2one("ir.attachment", ondelete="set null", index=True)
    migration_id = fields.Many2one("fastegy.drive.migration", ondelete="set null")
    kind = fields.Selection([("to_drive", "Move to Drive"), ("to_local", "Restore local"),
                             ("metadata", "Repair metadata")], required=True, default="to_drive")
    state = fields.Selection([("pending", "Pending"), ("retry", "Retry scheduled"),
        ("failed", "Needs attention"), ("done", "Done"), ("cancelled", "Cancelled")], default="pending", required=True, index=True)
    expected_checksum = fields.Char()
    attempts = fields.Integer(readonly=True)
    max_attempts = fields.Integer(default=5, required=True)
    next_attempt = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    error_category = fields.Char(readonly=True)
    completed_at = fields.Datetime(readonly=True)
    byte_count = fields.Float(readonly=True)

    @api.constrains("max_attempts")
    def _validate_attempt_limit(self):
        if any(not 1 <= job.max_attempts <= 20 for job in self):
            raise ValidationError(_("Use between one and twenty attempts per task."))

    @api.model
    def _enqueue(self, storage, attachment, kind="to_drive", migration=None):
        # تقفل هوية المرفق قبل البحث حتى لا تنشئ عمليتان مهمة مزدوجة.
        self.env.cr.execute("SELECT id FROM ir_attachment WHERE id=%s FOR UPDATE", [attachment.id])
        job = self.sudo().search([("storage_id", "=", storage.id), ("attachment_id", "=", attachment.id),
            ("kind", "=", kind), ("state", "in", ["pending", "retry"])], limit=1)
        return job or self.sudo().create({"storage_id": storage.id, "attachment_id": attachment.id,
            "kind": kind, "expected_checksum": attachment.checksum,
            "migration_id": migration.id if migration else False})

    def action_retry(self):
        self.env["fs.storage"]._fd_admin()
        self.filtered(lambda j: j.state in ("failed", "retry")).write({"state": "pending", "attempts": 0,
            "next_attempt": fields.Datetime.now(), "error_category": False})
        self.mapped("migration_id")._fd_update_queue_progress()

    def action_cancel(self):
        self.env["fs.storage"]._fd_admin()
        self.filtered(lambda j: j.state != "done").write({"state": "cancelled"})
        self.mapped("migration_id")._fd_update_queue_progress()

    def action_run(self):
        self.env["fs.storage"]._fd_admin()
        for job in self:
            self.env.cr.execute("SELECT id FROM fastegy_drive_job WHERE id=%s FOR UPDATE SKIP LOCKED", [job.id])
            if not self.env.cr.fetchone():
                continue
            job.invalidate_recordset()
            if job.state not in ("pending", "retry"):
                continue
            job.attempts += 1
            try:
                with self.env.cr.savepoint():
                    attachment = job.attachment_id.exists()
                    if not attachment:
                        job.state = "cancelled"
                        continue
                    attachment.check_access("read")
                    attachment.check_access("write")
                    attachment.check("read")
                    attachment.check("write")
                    self.env.cr.execute("SELECT id FROM ir_attachment WHERE id=%s FOR UPDATE", [attachment.id])
                    attachment.invalidate_recordset()
                    if job.expected_checksum and job.expected_checksum != attachment.checksum:
                        raise ValueError("Source changed since the task was queued")
                    if job.storage_id.fd_clone_readonly:
                        raise PermissionError("Copied database is read-only")
                    if job.kind == "metadata":
                        attachment._fd_sync_registry(force_metadata=True)
                    else:
                        target = job.storage_id.code if job.kind == "to_drive" else "file"
                        if job.kind == "to_drive" and (job.storage_id.fd_readonly or not job.storage_id.fd_allow_writes):
                            raise PermissionError("Uploads are disabled")
                        limit = job.storage_id.fd_max_file_mb * 1048576
                        if attachment.file_size > limit:
                            raise ValueError("File exceeds configured limit")
                        data = attachment.with_context(bin_size=False).raw
                        if len(data) > limit:
                            raise ValueError("File exceeds configured limit")
                        if len(data) != attachment.file_size or (attachment.checksum
                                and hashlib.sha1(data).hexdigest() != attachment.checksum):
                            raise ValueError("Source content failed integrity verification")
                        already_there = (attachment.store_fname or "").startswith(target + "://") if job.kind == "to_drive" else "://" not in (attachment.store_fname or "")
                        if not already_there:
                            fixed_path = uuid.uuid5(uuid.UUID(job.storage_id.fd_namespace),
                                "job:%s:%s:%s" % (job.id, job.kind, job.expected_checksum)).hex
                            values = {"raw": data}
                            if job.kind == "to_local":
                                values["fs_filename"] = False
                            attachment.sudo().with_context(storage_location=target, fd_job_fixed_path=fixed_path).write(values)
                        if attachment.raw != data or (job.kind == "to_drive" and attachment.db_datas):
                            raise ValueError("Destination verification failed")
                    job.write({"state": "done", "completed_at": fields.Datetime.now(), "error_category": False,
                               "byte_count": attachment.file_size})
                    job.storage_id._fd_event("job_" + job.kind, attachment, byte_count=attachment.file_size)
            except Exception as exc:
                category, transient = classify_error(exc)
                job.write({"state": "retry" if transient and job.attempts < job.max_attempts else "failed",
                    "error_category": category,
                    "next_attempt": fields.Datetime.now() + timedelta(seconds=min(3600, 30 * 2 ** job.attempts))})
                job.storage_id._fd_event("job_" + job.kind, outcome="error", category=category)
            job.migration_id._fd_update_queue_progress()
        return True

    @api.model
    def _cron_process(self, limit=10, scope_only=False):
        # معاملة منفصلة لكل مهمة؛ تعطل العامل لا يلغي نجاح المهام السابقة.
        domain = [("state", "in", ["pending", "retry"]), ("next_attempt", "<=", fields.Datetime.now())]
        if scope_only:
            domain += [("migration_id.fd_scope_storage_id", "!=", False)]
        else:
            domain += [("storage_id.fd_jobs_enabled", "=", True)]
        ids = self.sudo().search(domain, limit=limit).ids
        for job_id in ids:
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env[self._name].browse(job_id).action_run()
                cr.commit()
