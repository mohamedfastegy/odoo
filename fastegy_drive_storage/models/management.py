import json
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .fs_storage import PROTOCOL
from ..storage.policy import PREFIX, classify_error, encryption_keys, file_matches, parse_domain, seal, unseal


class DriveRule(models.Model):
    _name = "fastegy.drive.rule"
    _description = "Drive attachment routing rule"
    _order = "sequence, id"

    name = fields.Char(required=True)
    active = fields.Boolean(default=False)
    sequence = fields.Integer(default=10)
    storage_id = fields.Many2one("fs.storage", required=True, ondelete="restrict", domain=[("protocol", "=", PROTOCOL)])
    model_id = fields.Many2one("ir.model", required=True, ondelete="cascade")
    field_id = fields.Many2one("ir.model.fields", ondelete="cascade", domain="[('model_id', '=', model_id), ('ttype', '=', 'binary')]")
    company_id = fields.Many2one("res.company")
    mime_patterns = fields.Char(help="Comma separated MIME patterns, for example image/*,application/pdf")
    minimum_bytes = fields.Float(default=0)
    maximum_bytes = fields.Float(default=0, help="Zero means no additional rule limit; the storage file limit still applies.")
    record_domain = fields.Text(default="[]", required=True, help="A literal Odoo domain; no executable Python.")
    mode = fields.Selection([("drive", "اتصال درايف المحدد"), ("local", "تخزين أودو المحلي")], default="drive", required=True)
    test_record_id = fields.Integer(string="Preview record ID")
    test_mimetype = fields.Char(default="image/jpeg")
    test_size = fields.Float(default=1024)
    test_result = fields.Char(readonly=True)

    def action_test_rule(self):
        self.env["fs.storage"]._fd_admin()
        for rule in self:
            chosen = rule._choose({"res_model": rule.model_id.model, "res_id": rule.test_record_id,
                "res_field": rule.field_id.name if rule.field_id else False}, rule.test_size, rule.test_mimetype, rules=rule)
            rule.test_result = chosen or "لا توجد قاعدة مطابقة لهذه العينة"
        return True

    @api.constrains("record_domain", "model_id", "field_id", "minimum_bytes", "maximum_bytes")
    def _validate_rule(self):
        for rule in self:
            try:
                domain = parse_domain(rule.record_domain)
                self.env[rule.model_id.model]._where_calc(domain)
            except Exception:
                raise ValidationError(_("Use a valid literal domain for the selected model.")) from None
            if rule.field_id and rule.field_id.model_id != rule.model_id:
                raise ValidationError(_("The selected field must belong to the rule model."))
            if rule.minimum_bytes < 0 or (rule.maximum_bytes and rule.maximum_bytes < rule.minimum_bytes):
                raise ValidationError(_("Invalid file size interval."))

    @api.model
    def _choose(self, values, size, mimetype, rules=None):
        model_name = values.get("res_model")
        if not model_name or model_name not in self.env:
            return None
        record = self.env[model_name].browse(values.get("res_id") or 0).exists()
        if record:
            record.check_access("read")
        company = (record.company_id.id or self.env.company.id) if record and "company_id" in record._fields else self.env.company.id
        rules = rules if rules is not None else self.sudo().search([("model_id.model", "=", model_name)])
        for rule in rules:
            if rule.company_id and rule.company_id.id != company:
                continue
            if rule.field_id and rule.field_id.name != values.get("res_field"):
                continue
            if not file_matches(mimetype, size, rule.mime_patterns, rule.minimum_bytes, rule.maximum_bytes):
                continue
            if rule.record_domain != "[]" and (not record or not record.filtered_domain(parse_domain(rule.record_domain))):
                continue
            return "file" if rule.mode == "local" else rule.storage_id.code
        return None


class DriveManagement(models.Model):
    _name = "fs.storage"
    _inherit = ["fs.storage", "mail.thread", "mail.activity.mixin"]

    fd_shared_drive_id = fields.Char(string="Shared drive ID", copy=False)
    fd_folder_layout = fields.Selection([("flat", "المجلد الرئيسي فقط"), ("record", "الشركة ثم الموديل ثم السجل ثم الحقل")],
                                       default="flat", required=True)
    fd_retention_days = fields.Integer(default=30, required=True)
    fd_jobs_enabled = fields.Boolean(default=False)
    fd_monitor_enabled = fields.Boolean(default=False)
    fd_notify_user_id = fields.Many2one("res.users", domain=[("share", "=", False)])
    fd_health = fields.Selection([("unchecked", "لم يتم الفحص"), ("ok", "سليم"),
        ("warning", "يحتاج متابعة"), ("error", "تعذر الاتصال")], default="unchecked", readonly=True)
    fd_checked_at = fields.Datetime(readonly=True)
    fd_error_category = fields.Char(readonly=True)
    fd_quota_used = fields.Float(readonly=True, string="Account bytes used")
    fd_quota_limit = fields.Float(readonly=True, string="Account byte limit")
    fd_quota_used_mb = fields.Float(string="المساحة المستخدمة للحساب بالميجابايت",
        compute="_compute_fd_quota_mb", digits=(16, 0), help="الميجابايت يساوي مليون بايت؛ القيمة مقربة لأقرب عدد صحيح.")
    fd_quota_limit_mb = fields.Float(string="إجمالي مساحة الحساب بالميجابايت",
        compute="_compute_fd_quota_mb", digits=(16, 0), help="الميجابايت يساوي مليون بايت؛ القيمة مقربة لأقرب عدد صحيح.")
    fd_quota_known = fields.Boolean(readonly=True)
    fd_shared_verified = fields.Boolean(readonly=True)
    fd_encryption_state = fields.Char(compute="_compute_fd_encryption_state", groups="base.group_system")
    fd_oauth_lifecycle = fields.Selection([("testing", "تطبيق خارجي في وضع الاختبار"), ("production", "تطبيق خارجي في وضع التشغيل"),
        ("internal", "تطبيق داخلي للشركة")], default="testing")
    fd_picker_api_key = fields.Char(groups="base.group_system", copy=False)
    fd_picker_app_id = fields.Char(groups="base.group_system", copy=False)
    fd_blob_count = fields.Integer(compute="_compute_fd_metrics")
    fd_blob_bytes = fields.Float(compute="_compute_fd_metrics")
    fd_failed_jobs = fields.Integer(compute="_compute_fd_metrics")
    fd_orphan_count = fields.Integer(compute="_compute_fd_metrics")
    fd_reconcile_page_token = fields.Text(copy=False, groups="base.group_system")
    fd_reconcile_after_id = fields.Integer(copy=False)
    fd_reconcile_remote_done = fields.Boolean(copy=False)
    fd_reconcile_in_progress = fields.Boolean(copy=False, readonly=True)
    fd_reconciled_at = fields.Datetime(copy=False, readonly=True)

    @api.depends("fd_quota_used", "fd_quota_limit")
    def _compute_fd_quota_mb(self):
        # نغيّر وحدة العرض فقط، ونحتفظ بقيم البايت الأصلية للفحص والحسابات.
        for storage in self:
            storage.fd_quota_used_mb = int(storage.fd_quota_used / 1_000_000 + 0.5)
            storage.fd_quota_limit_mb = int(storage.fd_quota_limit / 1_000_000 + 0.5)

    @api.depends("fd_refresh_token", "fd_client_secret")
    def _compute_fd_encryption_state(self):
        for storage in self:
            values = [v for v in (storage.fd_refresh_token, storage.fd_client_secret) if v]
            storage.fd_encryption_state = ("مشفّر؛ يحتاج مفتاح التشفير الخارجي" if values and all(v.startswith(PREFIX) for v in values)
                                           else "بيانات قديمة غير مشفّرة؛ يلزم تشفيرها قبل التشغيل الفعلي")

    def _compute_fd_metrics(self):
        # Aggregate in PostgreSQL instead of loading every file into the dashboard worker.
        blob_groups = self.env["fastegy.drive.blob"].sudo()._read_group(
            [("storage_id", "in", self.ids)], ["storage_id", "state"], ["__count", "size:sum"])
        counts, sizes = {}, {}
        for storage, state, count, size in blob_groups:
            counts[(storage.id, state)] = count
            sizes[(storage.id, state)] = size
        job_groups = self.env["fastegy.drive.job"].sudo()._read_group(
            [("storage_id", "in", self.ids), ("state", "in", ["failed", "retry"])],
            ["storage_id"], ["__count"])
        failures = {storage.id: count for storage, count in job_groups}
        for storage in self:
            storage.fd_blob_count = counts.get((storage.id, "active"), 0)
            storage.fd_blob_bytes = sizes.get((storage.id, "active"), 0)
            storage.fd_orphan_count = counts.get((storage.id, "orphan"), 0)
            storage.fd_failed_jobs = failures.get(storage.id, 0)

    @api.constrains("fd_retention_days", "fd_shared_drive_id")
    def _fd_management_constraints(self):
        import re
        for storage in self:
            if storage.fd_retention_days < 1:
                raise ValidationError(_("Retain unreferenced files for at least one day."))
            if storage.fd_shared_drive_id and not re.fullmatch(r"[A-Za-z0-9_-]+", storage.fd_shared_drive_id):
                raise ValidationError(_("Invalid shared drive identifier."))

    @api.constrains("fd_oauth_lifecycle", "fd_allow_writes", "fd_client_secret", "fd_refresh_token")
    def _fd_production_security(self):
        for storage in self.filtered(lambda s: s.protocol == PROTOCOL and s.fd_allow_writes and s.fd_oauth_lifecycle != "testing"):
            if not encryption_keys() or not all(v and v.startswith(PREFIX) for v in (storage.fd_client_secret, storage.fd_refresh_token)):
                raise ValidationError(_("Encrypt credentials with the external hosting key before marking a writable connection as production/internal."))

    @api.model_create_multi
    def create(self, vals_list):
        cleaned = []
        for vals in vals_list:
            vals = dict(vals)
            for field in ("fd_client_secret", "fd_refresh_token"):
                if vals.get(field):
                    vals[field] = seal(vals[field])
            cleaned.append(vals)
        return super().create(cleaned)

    def write(self, vals):
        vals = dict(vals)
        if "fd_shared_drive_id" in vals:
            for storage in self:
                if vals["fd_shared_drive_id"] != storage.fd_shared_drive_id and self.env["ir.attachment"].sudo().search_count([
                    ("store_fname", "=like", storage._fd_store_pattern()), "|", ("res_field", "=", False), ("res_field", "!=", False)], limit=1):
                    raise ValidationError(_("A referenced storage cannot change its shared drive identity."))
            vals.update(fd_shared_verified=False, fd_connection_checked=False)
        for field in ("fd_client_secret", "fd_refresh_token"):
            if vals.get(field):
                vals[field] = seal(vals[field])
        return super().write(vals)

    def _fd_secret(self, field):
        self.ensure_one()
        if field not in ("fd_client_secret", "fd_refresh_token"):
            raise ValueError("Unsupported secret field")
        try:
            return unseal(self.sudo()[field])
        except ValueError as exc:
            raise UserError(str(exc)) from None

    def _get_fs_options(self):
        options = super()._get_fs_options()
        if self.protocol == PROTOCOL:
            options.update(client_secret=self._fd_secret("fd_client_secret"), refresh_token=self._fd_secret("fd_refresh_token"),
                shared_drive_id=self.fd_shared_drive_id or None, nested_layout=True)
        return options

    def _fd_client(self):
        client_options = self._get_fs_options().copy()
        nested = client_options.pop("nested_layout", False)
        client_options.pop("skip_instance_cache", None)
        from ..storage.drive_client import GoogleDriveClient
        client = GoogleDriveClient(**client_options)
        client.nested_layout = nested
        return client

    def action_fd_encrypt_credentials(self):
        self._fd_admin()
        self.ensure_one()
        try:
            values = {key: seal(unseal(self[key]), required=True) for key in ("fd_client_secret", "fd_refresh_token") if self[key]}
        except ValueError as exc:
            raise UserError(str(exc)) from None
        # إعادة تغليف القيم لا تلغي نجاح فحص الاتصال القائم.
        return self.with_context(fd_secret_rewrap=True).write(values)

    def _fd_lock_lifecycle(self):
        self.ensure_one()
        self.env.cr.execute("SELECT pg_advisory_xact_lock(%s, %s)", [194861, self.id])

    def _fd_event(self, operation, attachment=None, outcome="ok", category=False, byte_count=0):
        self.ensure_one()
        event = self.env["fastegy.drive.event"]._record(self, operation, attachment, outcome, category, byte_count)
        if outcome == "error":
            self.sudo().write({"fd_health": "warning", "fd_error_category": category})
        return event

    def action_fd_health(self):
        self._fd_admin()
        for storage in self:
            try:
                client = storage._fd_client()
                root = client.validate_root()
                quota = client.about().get("storageQuota", {})
                used, limit = float(quota.get("usage", 0)), float(quota.get("limit", 0))
                warning = bool(limit and used / limit >= 0.9)
                storage.write({"fd_health": "warning" if warning else "ok", "fd_checked_at": fields.Datetime.now(),
                    "fd_quota_used": used, "fd_quota_limit": limit, "fd_quota_known": bool(quota),
                    "fd_error_category": "quota_near_limit" if warning else False,
                    "fd_shared_verified": bool(storage.fd_shared_drive_id and root.get("driveId") == storage.fd_shared_drive_id)})
                storage._fd_event("health", outcome="warning" if warning else "ok", category="quota_near_limit" if warning else False)
            except Exception as exc:
                category, _transient = classify_error(exc)
                storage.write({"fd_health": "error", "fd_checked_at": fields.Datetime.now(), "fd_error_category": category})
                storage._fd_event("health", outcome="error", category=category)
            if storage.fd_notify_user_id and storage.fd_health in ("warning", "error"):
                model_id = self.env["ir.model"]._get_id("fs.storage")
                activities = self.env["mail.activity"].sudo()
                if not activities.search_count([("res_model_id", "=", model_id), ("res_id", "=", storage.id),
                    ("summary", "=", "Drive storage needs attention"), ("user_id", "=", storage.fd_notify_user_id.id)], limit=1):
                    activities.with_context(mail_activity_quick_update=True).create({"res_model_id": model_id, "res_id": storage.id,
                        "activity_type_id": self.env.ref("mail.mail_activity_data_todo").id,
                        "user_id": storage.fd_notify_user_id.id, "summary": "Drive storage needs attention",
                        "note": _("Open the storage health screen to review the connection, quota, or failed tasks.")})
        return True

    def action_fd_reconcile(self):
        self._fd_admin()
        self.ensure_one()
        self._fd_lock_lifecycle()
        client = self._fd_client()
        if not self.fd_reconcile_in_progress:
            self.write({"fd_reconcile_in_progress": True, "fd_reconcile_remote_done": False,
                        "fd_reconcile_page_token": False, "fd_reconcile_after_id": 0})
        if not self.fd_reconcile_remote_done:
            remotes, token = client.list_file_page(token=self.fd_reconcile_page_token or None, page_size=25)
            self.write({"fd_reconcile_page_token": token or False, "fd_reconcile_remote_done": not token})
        else:
            remotes = []
        for remote in remotes:
            path = client.file_path(remote)
            blob = self.env["fastegy.drive.blob"].search([("storage_id", "=", self.id), ("path", "=", path)], limit=1)
            if not blob:
                self.env["fastegy.drive.blob"].create({"storage_id": self.id, "path": path, "file_id": remote["id"],
                    "name": remote.get("name", path), "mimetype": remote.get("mimeType"), "size": float(remote.get("size", 0)),
                    "md5": remote.get("md5Checksum"), "state": "orphan", "orphan_since": fields.Datetime.now()})
        batch = self.env["fastegy.drive.blob"].search([
            ("storage_id", "=", self.id), ("id", ">", self.fd_reconcile_after_id)], order="id", limit=10)
        batch.action_reconcile()
        if batch:
            self.fd_reconcile_after_id = batch[-1].id
        if self.fd_reconcile_remote_done and len(batch) < 10:
            self.write({"fd_reconcile_in_progress": False, "fd_reconciled_at": fields.Datetime.now()})
        return self.action_fd_files()

    def action_fd_files(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "name": _("الملفات المدارة على درايف"), "res_model": "fastegy.drive.blob",
                "view_mode": "list,form", "domain": [("storage_id", "=", self.id)], "context": {"default_storage_id": self.id}}

    def action_fd_manifest(self):
        self._fd_admin()
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": "/fastegy_drive_storage/manifest/%s" % self.id, "target": "new"}

    @api.model
    def _cron_health(self):
        self.search([("protocol", "=", PROTOCOL), ("fd_monitor_enabled", "=", True)]).action_fd_health()
        # نحذف سجلات المتابعة القديمة فقط، وليس المرفقات أو ملفات درايف.
        cutoff = fields.Datetime.now() - timedelta(days=90)
        self.env["fastegy.drive.event"].sudo().search([("create_date", "<", cutoff)], limit=1000).unlink()
