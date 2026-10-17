import hashlib

from psycopg2.errors import LockNotAvailable

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .fs_storage import PROTOCOL


class DriveMigration(models.Model):
    _name = "fastegy.drive.migration"
    _description = "Scoped Drive migration trial"
    _order = "id desc"

    name = fields.Char(required=True, default="Drive trial")
    storage_id = fields.Many2one("fs.storage", required=True, ondelete="restrict",
                                 domain=[("protocol", "=", PROTOCOL)])
    # أودو يمنع restrict على البيانات الوصفية؛ حذف النطاق يلغي مهمته بدل توسيعها.
    model_id = fields.Many2one("ir.model", required=True, ondelete="cascade")
    field_id = fields.Many2one("ir.model.fields", ondelete="cascade",
                              domain="[('model_id', '=', model_id), ('ttype', '=', 'binary')]")
    include_field_attachments = fields.Boolean(
        string="Include binary/image field attachments", default=False,
        help="When no specific field is selected, leave this off to move only ordinary record attachments, excluding image fields and thumbnails.")
    record_id = fields.Integer(string="Only this business record ID", help="Set a record ID for the first test; zero selects the model scope.")
    direction = fields.Selection([("to_drive", "Odoo → Drive"), ("to_local", "Drive → local Odoo")],
                                 required=True, default="to_drive")
    batch_size = fields.Integer(default=1, required=True)
    state = fields.Selection([("draft", "Draft"), ("ready", "Preview ready"),
                              ("running", "Running"), ("paused", "Paused"),
                              ("error", "Needs attention"), ("queued", "Transfers queued"), ("done", "Done")],
                             default="draft", required=True, readonly=True)
    preview_at = fields.Datetime(readonly=True)
    max_attachment_id = fields.Integer(readonly=True)
    preview_count = fields.Integer(readonly=True)
    preview_bytes = fields.Float(readonly=True, string="Candidate bytes")
    last_attachment_id = fields.Integer(readonly=True)
    moved_count = fields.Integer(readonly=True)
    skipped_count = fields.Integer(readonly=True, string="Changed after preview")
    moved_bytes = fields.Float(readonly=True, string="Bytes verified and repointed")
    error_attachment_id = fields.Integer(readonly=True)
    error_message = fields.Text(readonly=True)
    queued_count = fields.Integer(readonly=True)

    def action_enqueue_batch(self):
        self._admin()
        self.ensure_one()
        if self.state not in ("ready", "running"):
            raise UserError(_("Preview or resume this migration first."))
        self.env.cr.execute("SELECT id FROM fastegy_drive_migration WHERE id=%s FOR UPDATE", [self.id])
        self.invalidate_recordset()
        for attachment in self.env["ir.attachment"].sudo().search(self._domain(), order="id", limit=500):
            self.env["fastegy.drive.job"]._enqueue(self.storage_id, attachment, self.direction, self)
            self.write({"last_attachment_id": attachment.id, "queued_count": self.queued_count + 1})
        self.state = "running" if self.env["ir.attachment"].sudo().search_count(self._domain(), limit=1) else "queued"
        return {"type": "ir.actions.act_window", "name": _("Independent transfer tasks"), "res_model": "fastegy.drive.job",
                "view_mode": "list,form", "domain": [("migration_id", "=", self.id)]}

    def _fd_update_queue_progress(self):
        for migration in self:
            jobs = self.env["fastegy.drive.job"].sudo().search([("migration_id", "=", migration.id)])
            if not jobs or migration.state not in ("queued", "error", "done"):
                continue
            done = jobs.filtered(lambda j: j.state == "done")
            pending = jobs.filtered(lambda j: j.state in ("pending", "retry"))
            failed = jobs.filtered(lambda j: j.state == "failed")
            migration.write({"moved_count": len(done), "moved_bytes": sum(done.mapped("byte_count")),
                "skipped_count": len(jobs.filtered(lambda j: j.state == "cancelled")),
                "state": "queued" if pending else ("error" if failed else "done")})

    def _admin(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only Settings administrators may migrate files."))

    @api.constrains("batch_size", "record_id", "field_id", "model_id")
    def _check_scope(self):
        for job in self:
            if not 1 <= job.batch_size <= 20 or job.record_id < 0:
                raise ValidationError(_("Batch size must be 1–20; record ID cannot be negative."))
            if job.field_id and job.field_id.model_id != job.model_id:
                raise ValidationError(_("Choose a field from the selected model."))
            if job.field_id:
                field = self.env[job.model_id.model]._fields.get(job.field_id.name)
                if not field or field.type != "binary" or not field.attachment or not field.store:
                    raise ValidationError(_("Only stored attachment-backed binary/image fields are supported."))

    def write(self, vals):
        scope = {"storage_id", "model_id", "field_id", "include_field_attachments", "record_id", "direction", "batch_size"}
        if scope.intersection(vals) and any(job.state != "draft" for job in self):
            raise UserError(_("Create a new job to change an already previewed scope."))
        return super().write(vals)

    def _domain(self, frozen=True):
        self.ensure_one()
        domain = [("type", "=", "binary"), ("res_model", "=", self.model_id.model),
                  ("public", "=", False), ("file_size", ">", 0),
                  ("mimetype", "not in", ["text/css", "application/javascript", "text/javascript"])]
        if self.field_id:
            domain.append(("res_field", "=", self.field_id.name))
        elif self.include_field_attachments:
            # أودو يخفي مرفقات الحقول من البحث ما لم نذكر الحقل صراحة.
            domain += ["|", ("res_field", "=", False), ("res_field", "!=", False)]
        else:
            domain.append(("res_field", "=", False))
        if self.record_id:
            domain.append(("res_id", "=", self.record_id))
        if self.direction == "to_drive":
            domain += ["|", ("store_fname", "=", False), ("store_fname", "not like", "://")]
        else:
            domain.append(("store_fname", "=like", self.storage_id._fd_store_pattern()))
        if frozen:
            domain += [("id", "<=", self.max_attachment_id),
                       ("id", ">", self.last_attachment_id),
                       ("write_date", "<=", self.preview_at)]
        return domain

    def action_preview(self):
        self._admin()
        self.ensure_one()
        if self.state != "draft":
            raise UserError(_("This job has already been previewed. Create a new job for a new snapshot."))
        attachments = self.env["ir.attachment"].sudo()
        domain = self._domain(frozen=False)
        top = attachments.search(domain, order="id desc", limit=1)
        summary = attachments.read_group(domain, ["file_size:sum"], [])
        # ORM write_date uses the cursor timestamp, including microseconds.
        # A second-truncated wall clock can exclude files created in this transaction.
        self.write({"preview_at": self.env.cr.now(), "max_attachment_id": top.id or 0,
                    "preview_count": attachments.search_count(domain),
                    "preview_bytes": summary[0].get("file_size", 0) if summary else 0,
                    "state": "ready"})
        return True

    def action_pause(self):
        self._admin()
        self.filtered(lambda j: j.state == "running").write({"state": "paused"})

    def action_resume(self):
        self._admin()
        self.filtered(lambda j: j.state in ("ready", "paused", "error")).write(
            {"state": "running", "error_message": False, "error_attachment_id": 0})

    def _move_one(self, attachment):
        self.ensure_one()
        # قفل السجل يمنع استبدال صورة المستخدم بينما نرحل نسختها السابقة.
        self.env.cr.execute("SELECT id FROM ir_attachment WHERE id = %s FOR UPDATE", (attachment.id,))
        attachment.invalidate_recordset()
        if attachment.write_date > self.preview_at:
            return False
        limit = self.storage_id.fd_max_file_mb * 1024 * 1024
        if attachment.file_size > limit:
            raise UserError(_("File exceeds the trial memory limit. It was not read or moved."))
        data = attachment.with_context(bin_size=False).raw
        if (not data or len(data) != attachment.file_size
                or hashlib.sha1(data).hexdigest() != attachment.checksum):
            raise UserError(_("Source content does not match the stored size/checksum."))
        if len(data) > limit:
            raise UserError(_("File exceeds the trial memory limit. It was not moved."))
        destination = self.storage_id.code if self.direction == "to_drive" else "file"
        values = {"raw": data}
        if self.direction == "to_local":
            values["fs_filename"] = False
        attachment.with_context(storage_location=destination, bin_size=False).write(values)
        attachment.invalidate_recordset(["raw", "datas", "db_datas"])
        received = attachment.with_context(bin_size=False).raw
        if received != data:
            raise UserError(_("Destination verification failed. The source reference will be restored."))
        expected_remote = self.storage_id.code + "://"
        if self.direction == "to_drive" and (
                not attachment.store_fname or not attachment.store_fname.startswith(expected_remote)
                or attachment.db_datas):
            raise UserError(_("The attachment did not move completely to the selected Drive storage."))
        if self.direction == "to_local" and (not attachment.store_fname or "://" in attachment.store_fname):
            raise UserError(_("The attachment was not restored to the local filestore."))
        if attachment.res_field and attachment.res_model in self.env:
            record = self.env[attachment.res_model].browse(attachment.res_id).exists()
            if record and attachment.res_field in record._fields:
                record.invalidate_recordset([attachment.res_field])
        return len(data)

    def action_run_batch(self):
        self._admin()
        self.ensure_one()
        if self.state not in ("ready", "running"):
            raise UserError(_("Preview or resume this job first."))
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute("SELECT id FROM fastegy_drive_migration WHERE id = %s FOR UPDATE NOWAIT", (self.id,))
        except LockNotAvailable as exc:
            raise UserError(_("Another worker is already processing this job.")) from exc
        self.invalidate_recordset()
        if self.state not in ("ready", "running"):
            return False
        storage = self.storage_id
        if storage.fd_clone_readonly:
            raise UserError(_("Migration is blocked on a copy of the original database."))
        if self.direction == "to_drive" and (storage.fd_readonly or not storage.fd_allow_writes or not storage.fd_connection_checked):
            raise UserError(_("Test the Drive connection and allow uploads before migrating."))
        candidates = self.env["ir.attachment"].sudo().search(self._domain(), order="id", limit=self.batch_size)
        self.state = "running"
        for attachment in candidates:
            try:
                with self.env.cr.savepoint():
                    size = self._move_one(attachment)
                    self.write({"last_attachment_id": attachment.id,
                                "moved_count": self.moved_count + (1 if size else 0),
                                "skipped_count": self.skipped_count + (0 if size else 1),
                                "moved_bytes": self.moved_bytes + (size or 0)})
            except Exception:
                # لا نسجل نص رد جوجل أو رموز الدخول داخل سجل الأخطاء.
                self.write({"state": "error", "error_attachment_id": attachment.id,
                            "error_message": _("Move/verification failed for attachment %s. Source changes were rolled back; inspect the connection and file limit before resuming.") % attachment.id})
                break
        if self.state == "running" and not self.env["ir.attachment"].sudo().search_count(self._domain(), limit=1):
            self.state = "done"
        return True

    @api.model
    def _cron_run_batches(self):
        # مهمة خاملة عند التثبيت؛ كل تشغيل ينفذ دفعة واحدة فقط في معاملة أودو.
        for job in self.search([("state", "=", "running")], limit=1, order="id"):
            job.action_run_batch()

