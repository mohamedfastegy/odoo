"""Application selection and persistent, provenance-based attachment routing."""
import json
import uuid

from odoo import _, api, fields, models, tools, SUPERUSER_ID
from odoo.exceptions import UserError, ValidationError

from .fs_storage import PROTOCOL
from ..storage.scope_policy import discover_scope, scope_fingerprint, scope_key


REASONS = {
    "owned_model": "موديل أنشأته الإضافة؛ يشمل المرفقات العادية وحقول المرفقات المدعومة.",
    "introduced_field": "حقل أنشأته الإضافة على موديل مشترك؛ التوجيه لهذا الحقل فقط.",
    "shared_model": "موديل مشترك؛ مرفقاته العامة خارج الاكتشاف التلقائي.",
    "direct_column": "المحتوى محفوظ في عمود بقاعدة البيانات؛ يحتاج موصلًا مخصصًا.",
    "nonstored": "حقل غير محفوظ؛ يلزم فحص مصدر المحتوى قبل اعتباره ملفًا مرفوعًا.",
    "delegated_field": "الحقل يُحال إلى موديل آخر؛ يلزم توجيه مصدره الفعلي.",
    "overridden_shared_field": "الإضافة عدّلت حقلًا مشتركًا قائمًا؛ لا يثبت ذلك ملكيتها لكل ملفاته.",
    "system_or_temporary": "موديل للبنية الداخلية أو مؤقت أو تقرير؛ يلزم نطاق صريح منفصل.",
    "missing_module": "الإضافة لم تعد مثبتة؛ لا يُفعّل توجيه جديد لها.",
}
READY = "ready"
PROTECTED = {"fd_scope_active", "fd_scope_applied_modules", "fd_scope_backfill_applied",
             "fd_scope_fingerprint", "fd_scope_checked_at", "fd_scope_summary", "fd_scope_error", "fd_scope_epoch"}


class DriveScopeLine(models.Model):
    _name = "fastegy.drive.scope.line"
    _description = "Discovered application file storage scope"
    _order = "status, model_name, kind, field_name"

    storage_id = fields.Many2one("fs.storage", required=True, ondelete="cascade", index=True)
    scope_key = fields.Char(required=True, index=True)
    route_key = fields.Char(index=True)
    model_name = fields.Char(index=True)
    field_name = fields.Char()
    label = fields.Char()
    kind = fields.Selection([("model", "موديل"), ("field", "حقل ملف"), ("module", "إضافة")], required=True)
    source_modules = fields.Char(string="الإضافات المصدر")
    status = fields.Selection([("ready", "جاهز للتوجيه"), ("shared", "موديل مشترك"),
        ("column", "يحتاج موصلًا لقاعدة البيانات"), ("computed", "غير محفوظ / محسوب"),
        ("review", "يحتاج مراجعة المسار"), ("conflict", "تعارض مخزن"),
        ("excluded", "خارج النطاق"), ("missing", "إضافة غير مثبتة"), ("retired", "نطاق سابق")], required=True)
    reason = fields.Text(string="السبب")
    current = fields.Boolean(default=True, index=True)
    _sql_constraints = [
        ("scope_storage_key_unique", "unique(storage_id, scope_key)", "An entry already exists for this storage scope."),
        ("scope_route_unique", "unique(route_key)", "An automatic route already belongs to another storage."),
    ]


class DriveApplicationScope(models.Model):
    _inherit = "fs.storage"

    fd_scope_module_ids = fields.Many2many("ir.module.module", "fd_storage_module_rel", "storage_id", "module_id",
        string="الإضافات المثبتة", domain=[("state", "=", "installed")], copy=False)
    fd_scope_auto_update = fields.Boolean(string="متابعة تحديثات الإضافات تلقائيًا", default=True)
    fd_scope_include_existing = fields.Boolean(string="نقل الملفات القديمة أيضًا", default=False,
        help="يُطبق بعد الضغط على اكتشاف وتفعيل. ينقل المرفقات الخاصة المدعومة على دفعات ويتحقق منها.")
    fd_scope_active = fields.Boolean(string="التوجيه التلقائي مفعّل", readonly=True, copy=False)
    fd_scope_applied_modules = fields.Text(readonly=True, copy=False)
    fd_scope_backfill_applied = fields.Boolean(readonly=True, copy=False)
    fd_scope_epoch = fields.Char(readonly=True, copy=False)
    fd_scope_fingerprint = fields.Char(readonly=True, copy=False)
    fd_scope_checked_at = fields.Datetime(string="آخر فحص", readonly=True, copy=False)
    fd_scope_summary = fields.Text(string="نتيجة الاكتشاف", readonly=True, copy=False)
    fd_scope_error = fields.Char(string="حالة المتابعة", readonly=True, copy=False)
    fd_scope_dirty = fields.Boolean(compute="_compute_fd_scope_dirty")
    fd_scope_line_ids = fields.One2many("fastegy.drive.scope.line", "storage_id", string="تفاصيل النطاق", copy=False)

    @api.depends("fd_scope_module_ids", "fd_scope_module_ids.state", "fd_scope_applied_modules",
                 "fd_scope_include_existing", "fd_scope_backfill_applied")
    def _compute_fd_scope_dirty(self):
        for storage in self:
            applied = set(json.loads(storage.fd_scope_applied_modules or "[]"))
            storage.fd_scope_dirty = (set(storage.fd_scope_module_ids.mapped("name")) != applied
                or storage.fd_scope_include_existing != storage.fd_scope_backfill_applied)

    @api.model_create_multi
    def create(self, vals_list):
        if any(any(vals.get(key) for key in PROTECTED) for vals in vals_list):
            raise ValidationError(_("Use Discover and Activate to create an automatic scope."))
        return super().create(vals_list)

    def write(self, vals):
        if PROTECTED.intersection(vals):
            raise ValidationError(_("Discovery results can only be changed by scope actions."))
        if {"fd_scope_module_ids", "fd_scope_auto_update", "fd_scope_include_existing"}.intersection(vals):
            self._fd_admin()
        result = super().write(vals)
        if vals.get("fd_scope_auto_update") and self.filtered("fd_scope_active"):
            self.env.ref("fastegy_drive_storage.cron_drive_scope").sudo().write({"active": True})
        if vals.get("fd_scope_include_existing") is False:
            self._fd_scope_write({"fd_scope_backfill_applied": False})
            self._fd_scope_cancel_backfill()
        return result

    def _fd_scope_write(self, vals):
        return super(DriveApplicationScope, self).write(vals)

    def _fd_scope_lock(self, nonblocking=False):
        # يمنع اكتشافين متزامنين من الاستحواذ على نفس المسار بين مخزنين.
        if nonblocking:
            self.env.cr.execute("SELECT pg_try_advisory_xact_lock(17481223, 210)")
            if not self.env.cr.fetchone()[0]:
                return False
        else:
            self.env.cr.execute("SELECT pg_advisory_xact_lock(17481223, 210)")
        if self.ids:
            self.env.cr.execute("SELECT id FROM fs_storage WHERE id IN %s ORDER BY id FOR UPDATE", [tuple(self.ids)])
            self.invalidate_recordset()
        return True

    @api.model
    def _fd_scope_descriptors(self):
        descriptors = []
        for name, model in self.env.registry.models.items():
            binary_fields = []
            for field_name, field in model._fields.items():
                if field.type != "binary":
                    continue
                # أقدم تعريف متوافق يحدد منشأ الحقل؛ تعديل عنوانه لا ينقل ملكيته.
                origin = None
                for cls in reversed(model._model_classes__):
                    definition = cls.__dict__.get(field_name)
                    if isinstance(definition, fields.Field):
                        if not isinstance(field, type(definition)):
                            origin = None
                            continue
                        if origin is None:
                            origin = cls._module
                binary_fields.append({"name": field_name, "label": str(field.string or field_name),
                    "store": bool(field.store), "attachment": bool(field.attachment),
                    "inherited": bool(field.inherited), "origin": origin,
                    "modules": sorted(field._modules or ())})
            descriptors.append({"name": name, "label": str(model._description),
                "owner": model._original_module, "auto": model._auto,
                "abstract": model._abstract, "transient": model._transient, "fields": binary_fields})
        return descriptors

    def _fd_scope_plan(self, names):
        self.ensure_one()
        modules = self.env["ir.module.module"].sudo().search([("name", "in", list(names)), ("state", "=", "installed")])
        installed = set(modules.mapped("name"))
        rows = discover_scope(installed, self._fd_scope_descriptors())
        for name in sorted(set(names) - installed):
            rows.append({"scope_key": "@module/" + name, "model_name": False, "field_name": False,
                "label": name, "kind": "module", "source_modules": name, "status": "missing", "reason": "missing_module"})
        storages = self.sudo().search([])
        manual_models, manual_fields = {}, {}
        for storage in storages:
            for model in storage.model_ids:
                manual_models[model.model] = storage
            for field in storage.field_ids:
                manual_fields[scope_key(field.model, field.name)] = storage
        other_routes = {line.route_key: line.storage_id for line in self.env["fastegy.drive.scope.line"].sudo().search([
            ("route_key", "!=", False), ("storage_id", "!=", self.id), ("storage_id.fd_scope_active", "=", True)])}
        for row in rows:
            if row["status"] != READY:
                continue
            key, model_key = row["scope_key"], scope_key(row["model_name"])
            owners = [manual_fields.get(key) if row["kind"] == "field" else None,
                      manual_models.get(row["model_name"]), other_routes.get(key), other_routes.get(model_key)]
            owner = next((storage for storage in owners if storage and storage.id != self.id), None)
            if owner:
                row.update(status="conflict", reason="existing_storage:" + owner.code)
        versions = [m.name + "@" + (m.installed_version or "") for m in modules]
        return rows, scope_fingerprint(versions + sorted(set(names) - installed), rows)

    def _fd_scope_apply_plan(self, names):
        self.ensure_one()
        rows, fingerprint = self._fd_scope_plan(names)
        self._fd_scope_write({"fd_scope_checked_at": fields.Datetime.now(), "fd_scope_error": False})
        if fingerprint == self.fd_scope_fingerprint:
            return False
        Line = self.env["fastegy.drive.scope.line"].sudo()
        Line.search([("route_key", "!=", False), ("storage_id.fd_scope_active", "=", False)]).write({"route_key": False})
        existing = {line.scope_key: line for line in Line.search([("storage_id", "=", self.id)])}
        # تُفرغ المفاتيح أولًا لكي يكون تعديل نطاق كامل ذريًا ولا يصطدم بصف سابق.
        self.fd_scope_line_ids.sudo().write({"route_key": False, "current": False, "status": "retired"})
        for row in rows:
            row = dict(row)
            row.update(storage_id=self.id, current=True,
                route_key=row["scope_key"] if row["status"] == READY and self.fd_scope_active else False,
                reason=REASONS.get(row["reason"], row["reason"]))
            if row["scope_key"] in existing:
                existing[row["scope_key"]].write(row)
            else:
                Line.create(row)
        ready_models = sum(row["status"] == READY and row["kind"] == "model" for row in rows)
        ready_fields = sum(row["status"] == READY and row["kind"] == "field" for row in rows)
        exceptions = sum(row["status"] in ("column", "review", "conflict", "missing") for row in rows)
        summary = _("Ready models: %(models)s; attachment fields: %(fields)s; exceptions: %(exceptions)s. "
                    "Counts are definitions, not files. Shared models, nonstored fields and excluded infrastructure are listed separately.") % {
                        "models": ready_models, "fields": ready_fields, "exceptions": exceptions}
        self._fd_scope_write({"fd_scope_fingerprint": fingerprint, "fd_scope_summary": summary})
        self.env.registry.clear_cache()
        self._fd_event("scope_refresh", category="exceptions" if exceptions else "ready")
        return True

    def action_fd_scope_activate(self):
        self._fd_admin()
        self.ensure_one()
        self._fd_scope_lock()
        if self.protocol != PROTOCOL or self.fd_clone_readonly:
            raise UserError(_("Activate discovery only on the original Drive connection."))
        if not self.fd_connection_checked or not self.fd_allow_writes or self.fd_readonly:
            raise UserError(_("Test the folder and enable uploads before activating discovery."))
        if not self.fd_scope_module_ids or any(m.state != "installed" for m in self.fd_scope_module_ids):
            raise UserError(_("Select one or more installed applications."))
        names = sorted(self.fd_scope_module_ids.mapped("name"))
        epoch = (self.fd_scope_epoch if self.fd_scope_active and self.fd_scope_backfill_applied else False) or uuid.uuid4().hex
        self._fd_scope_write({"fd_scope_active": True, "fd_scope_applied_modules": json.dumps(names),
            "fd_scope_backfill_applied": self.fd_scope_include_existing,
            "fd_scope_epoch": epoch if self.fd_scope_include_existing else False,
            "fd_scope_fingerprint": False})
        self._fd_scope_apply_plan(names)
        if self.fd_scope_include_existing:
            self._fd_scope_prepare_backfill()
            self.env.ref("fastegy_drive_storage.cron_drive_scope_jobs").sudo().write({"active": True})
        else:
            self._fd_scope_cancel_backfill()
        if self.fd_scope_auto_update or self.fd_scope_include_existing:
            self.env.ref("fastegy_drive_storage.cron_drive_scope").sudo().write({"active": True})
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("Application storage scope activated"), "message": self.fd_scope_summary,
            "type": "warning" if self.fd_scope_line_ids.filtered(lambda l: l.current and l.status in (
                "column", "review", "conflict", "missing")) else "success", "sticky": True}}

    def action_fd_scope_refresh(self):
        self._fd_admin()
        self.ensure_one()
        if not self.fd_scope_active or self.fd_clone_readonly:
            raise UserError(_("Activate the original connection's application scope first."))
        self._fd_scope_lock()
        self._fd_scope_apply_plan(json.loads(self.fd_scope_applied_modules or "[]"))
        return True

    def action_fd_scope_pause(self):
        self._fd_admin()
        self._fd_scope_lock()
        self.fd_scope_line_ids.sudo().write({"route_key": False})
        self._fd_scope_write({"fd_scope_active": False, "fd_scope_fingerprint": False})
        self._fd_scope_cancel_backfill()
        self.env.registry.clear_cache()
        return True

    def action_fd_scope_report(self):
        self._fd_admin()
        self.ensure_one()
        return {"type": "ir.actions.act_window", "name": _("Application coverage and exceptions"),
            "res_model": "fastegy.drive.scope.line", "view_mode": "list,form",
            "domain": [("storage_id", "=", self.id), ("current", "=", True)]}

    def action_fd_scope_backfill_report(self):
        self._fd_admin()
        self.ensure_one()
        return {"type": "ir.actions.act_window", "name": _("Application backfill snapshots"),
            "res_model": "fastegy.drive.migration", "view_mode": "list,form",
            "domain": [("fd_scope_storage_id", "=", self.id)]}

    @api.model
    @tools.ormcache("model_name", "field_name")
    def _fd_scope_lookup(self, model_name, field_name=None):
        if not model_name or model_name not in self.env:
            return None
        if field_name:
            field = self.env[model_name]._fields.get(field_name)
            if not field or field.type != "binary" or not field.store or not field.attachment or field.inherited:
                return None
        keys = [scope_key(model_name, field_name), scope_key(model_name)]
        routes = self.env["fastegy.drive.scope.line"].sudo().search([("route_key", "in", keys),
            ("storage_id.fd_scope_active", "=", True), ("storage_id.protocol", "=", PROTOCOL)])
        exact = routes.filtered(lambda line: line.route_key == keys[0])
        chosen = exact[:1] or routes[:1]
        return chosen.storage_id.code if chosen else None

    @api.model
    def get_storage_code_by_model_field(self, model_name, field_name=None):
        # التخصيص اليدوي القائم يسبق النطاق المكتشف، فلا نستولي على مخزن آخر.
        return super().get_storage_code_by_model_field(model_name, field_name) or self._fd_scope_lookup(model_name, field_name)

    def _fd_scope_target(self, attachment):
        self.ensure_one()
        if not self.fd_scope_active or not self._fd_scope_lookup(attachment.res_model, attachment.res_field or None) == self.code:
            return None
        values = {"res_model": attachment.res_model, "res_id": attachment.res_id, "res_field": attachment.res_field}
        override = self.env["fastegy.drive.rule"]._choose(values, attachment.file_size, attachment.mimetype)
        return override or self.get_storage_code_by_model_field(attachment.res_model, attachment.res_field or None)

    def _fd_scope_cancel_backfill(self):
        # إبطال الجيل يلغي العمل عند فحص العامل، دون قفل المهام داخل معاملة الإعداد.
        self._fd_scope_write({"fd_scope_epoch": False})

    def _fd_scope_generation(self):
        self.ensure_one()
        return (self.fd_scope_fingerprint or "") + ":" + (self.fd_scope_epoch or "")

    def _fd_scope_prepare_backfill(self):
        self.ensure_one()
        if not self.fd_scope_active or not self.fd_scope_backfill_applied or self.fd_clone_readonly:
            return
        models = self.env["ir.model"].sudo()
        Migration = self.env["fastegy.drive.migration"].sudo()
        routes = self.fd_scope_line_ids.filtered(lambda l: l.route_key and l.status == READY)
        full_models = set(routes.filtered(lambda l: l.kind == "model").mapped("model_name"))
        for line in routes:
            if line.kind == "field" and line.model_name in full_models:
                continue
            if Migration.search_count([("fd_scope_storage_id", "=", self.id), ("fd_scope_key", "=", line.scope_key),
                ("fd_scope_plan_hash", "=", self._fd_scope_generation()), ("state", "!=", "paused")], limit=1):
                continue
            model = models.search([("model", "=", line.model_name)], limit=1)
            if not model:
                continue
            field = self.env["ir.model.fields"].sudo().search([("model_id", "=", model.id),
                ("name", "=", line.field_name)], limit=1) if line.field_name else False
            values = {"name": "Application backfill: " + line.scope_key, "storage_id": self.id, "model_id": model.id,
                "field_id": field.id if field else False, "include_field_attachments": True,
                "fd_scope_storage_id": self.id, "fd_scope_key": line.scope_key,
                "fd_scope_plan_hash": self._fd_scope_generation()}
            candidate = Migration.new(values)
            if not self.env["ir.attachment"].sudo().search_count(candidate._domain(frozen=False), limit=1):
                continue
            migration = Migration.create(values)
            migration.action_preview()

    @api.model
    def _cron_fd_scope(self):
        ids = self.sudo().search([("fd_scope_active", "=", True)], order="fd_scope_checked_at, id", limit=5).ids
        for storage_id in ids:
            # تُعتمد بيانات النطاق قبل قفل المرفقات في معاملة منفصلة للطابور.
            # هذا يمنع دورة قفل مخزن→مرفق مقابل العامل الذي يقفل مرفق→سجل المخزن.
            try:
                with self.env.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    storage = env["fs.storage"].browse(storage_id)
                    if storage.fd_clone_readonly or not storage.fd_scope_active or not storage._fd_scope_lock(nonblocking=True):
                        continue
                    if storage.fd_scope_auto_update:
                        storage._fd_scope_apply_plan(json.loads(storage.fd_scope_applied_modules or "[]"))
                    else:
                        storage._fd_scope_write({"fd_scope_checked_at": fields.Datetime.now()})
                    if storage.fd_scope_backfill_applied:
                        storage._fd_scope_prepare_backfill()
                    cr.commit()
                with self.env.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    storage = env["fs.storage"].browse(storage_id)
                    migration = env["fastegy.drive.migration"].search([
                        ("fd_scope_storage_id", "=", storage_id), ("fd_scope_plan_hash", "=", storage._fd_scope_generation()),
                        ("state", "in", ["ready", "running"])], limit=1)
                    if migration:
                        migration._fd_scope_enqueue_batch()
                    cr.commit()
            except Exception:
                with self.env.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    storage = env["fs.storage"].browse(storage_id)
                    storage._fd_scope_write({"fd_scope_checked_at": fields.Datetime.now(),
                        "fd_scope_error": _("Scope update failed; previous committed routes were retained. Use Refresh to retry.")})
                    storage._fd_event("scope_refresh", outcome="error", category="scope_update")
                    cr.commit()


class DriveScopeMigration(models.Model):
    _inherit = "fastegy.drive.migration"

    fd_scope_storage_id = fields.Many2one("fs.storage", ondelete="restrict", index=True, copy=False)
    fd_scope_key = fields.Char(copy=False)
    fd_scope_plan_hash = fields.Char(copy=False)
    fd_scope_skipped = fields.Integer(readonly=True)

    def _fd_scope_enqueue_batch(self):
        self.ensure_one()
        storage = self.fd_scope_storage_id
        if not storage or not storage.fd_scope_active or not storage.fd_scope_backfill_applied or storage.fd_clone_readonly:
            return False
        if self.fd_scope_plan_hash != storage._fd_scope_generation():
            self.state = "paused"
            return False
        self.env.cr.execute("SELECT id FROM fastegy_drive_migration WHERE id=%s FOR UPDATE", [self.id])
        self.invalidate_recordset()
        if self.state not in ("ready", "running"):
            return False
        for attachment in self.env["ir.attachment"].sudo().search(self._domain(), order="id", limit=100):
            self.env.cr.execute("SELECT id FROM ir_attachment WHERE id=%s FOR UPDATE SKIP LOCKED", [attachment.id])
            if not self.env.cr.fetchone():
                self.state = "running"
                return True
            if storage._fd_scope_target(attachment) == storage.code:
                job = self.env["fastegy.drive.job"]._enqueue(storage, attachment, "to_drive", self)
                if job.migration_id.fd_scope_storage_id and (job.migration_id.state == "paused"
                        or job.migration_id.fd_scope_plan_hash != storage._fd_scope_generation()):
                    job.action_run()  # يلغي مشغل النطاق المهمة القديمة تحت قفلها أولًا.
                    job.invalidate_recordset()
                    if job.state in ("pending", "retry"):
                        self.state = "running"
                        return True
                    job = self.env["fastegy.drive.job"]._enqueue(storage, attachment, "to_drive", self)
                if job.migration_id == self:
                    self.queued_count += 1
                else:
                    self.fd_scope_skipped += 1
            else:
                self.fd_scope_skipped += 1
            self.last_attachment_id = attachment.id
        if self.env["ir.attachment"].sudo().search_count(self._domain(), limit=1):
            self.state = "running"
        else:
            self.state = "queued" if self.queued_count else "done"
            self._fd_update_queue_progress()
        return True

    def action_enqueue_batch(self):
        self._admin()
        self.ensure_one()
        if not self.fd_scope_storage_id:
            return super().action_enqueue_batch()
        self._fd_scope_enqueue_batch()
        return {"type": "ir.actions.act_window", "name": _("Application transfer tasks"),
            "res_model": "fastegy.drive.job", "view_mode": "list,form", "domain": [("migration_id", "=", self.id)]}

    def action_run_batch(self):
        self._admin()
        self.ensure_one()
        if self.fd_scope_storage_id:
            return self.action_enqueue_batch()
        return super().action_run_batch()


class DriveScopeJob(models.Model):
    _inherit = "fastegy.drive.job"

    def action_run(self):
        self.env["fs.storage"]._fd_admin()
        ordinary = self.browse()
        for job in self:
            self.env.cr.execute("SELECT id FROM fastegy_drive_job WHERE id=%s FOR UPDATE SKIP LOCKED", [job.id])
            if not self.env.cr.fetchone():
                continue
            job.invalidate_recordset()
            storage = job.migration_id.fd_scope_storage_id
            if storage and job.state in ("pending", "retry") and (not storage.fd_scope_backfill_applied
                    or job.migration_id.state == "paused" or storage.fd_clone_readonly
                    or job.migration_id.fd_scope_plan_hash != storage._fd_scope_generation()
                    or storage._fd_scope_target(job.attachment_id) != storage.code):
                job.action_cancel()
            else:
                ordinary |= job
        return super(DriveScopeJob, ordinary).action_run()

    @api.model
    def _cron_fd_scope_jobs(self):
        # لا يفعّل هذا المشغل مهام يدوية أخرى على المخزن نفسه.
        self._cron_process(limit=5, scope_only=True)
        stale = self.env["fastegy.drive.migration"].sudo().search([
            ("fd_scope_storage_id", "!=", False), ("state", "in", ["ready", "running", "queued", "error"])])
        stale = stale.filtered(lambda m: not m.fd_scope_storage_id.fd_scope_active
            or not m.fd_scope_storage_id.fd_scope_backfill_applied
            or m.fd_scope_plan_hash != m.fd_scope_storage_id._fd_scope_generation())
        stale[:5].write({"state": "paused"})
