"""Scope policy, real Odoo routing and isolated backfill transaction regressions."""
import json
import uuid
from unittest.mock import patch

import fsspec

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "fastegy_drive_trial", "fastegy_drive_scope")
class TestApplicationScope(TransactionCase):
    def setUp(self):
        super().setUp()
        self.module = self.env["ir.module.module"].search([("name", "=", "mail"), ("state", "=", "installed")], limit=1)
        self.storage = self._storage()
        self.partner = self.env["res.partner"].with_context(mail_create_nosubscribe=True).create({"name": "Isolated scope test"})
        self.remote = fsspec.filesystem("memory", skip_instance_cache=True)
        original = type(self.storage)._get_filesystem
        storage_id, remote = self.storage.id, self.remote
        def filesystem(record):
            return remote if record.id == storage_id else original(record)
        p = patch.object(type(self.storage), "_get_filesystem", filesystem)
        p.start()
        self.addCleanup(p.stop)
        self.storage.fd_scope_module_ids = self.module

    def _storage(self):
        return self.env["fs.storage"].create({"name": "Scope isolated", "code": "scope_" + uuid.uuid4().hex,
            "protocol": "fastegy_gdrive", "fd_client_id": "fixture", "fd_client_secret": "fixture",
            "fd_refresh_token": "fixture", "fd_root_folder_id": "fixture", "fd_connection_checked": True,
            "fd_allow_writes": True, "fd_readonly": False})

    def _descriptors(self, shared=False, extra=None):
        fields_ = [{"name": "image_1920", "store": True, "attachment": True,
                    "modules": ["mail"], "origin": "mail"}]
        if extra:
            fields_.append(extra)
        return [{"name": "res.partner", "owner": "base" if shared else "mail", "auto": True,
                 "abstract": False, "transient": False, "fields": fields_}]

    def _activate(self, storage=None, shared=False, extra=None):
        storage = storage or self.storage
        with patch.object(type(storage), "_fd_scope_descriptors", return_value=self._descriptors(shared, extra)):
            storage.action_fd_scope_activate()

    def _attachment(self, local=False):
        Attachment = self.env["ir.attachment"]
        if local:
            Attachment = Attachment.with_context(storage_location="file")
        return Attachment.create({"name": "scope proof.txt", "raw": b"scope synthetic proof",
            "res_model": "res.partner", "res_id": self.partner.id, "mimetype": "text/plain"})

    def _scoped_backfill(self):
        # تُقصر قراءات/نقل الاختبار على السجل الصناعي؛ لا تُقرأ ملفات الأعمال.
        original = type(self.env["fastegy.drive.migration"])._domain
        partner_id = self.partner.id
        def domain(migration, frozen=True):
            return original(migration, frozen=frozen) + [("res_id", "=", partner_id)]
        p = patch.object(type(self.env["fastegy.drive.migration"]), "_domain", domain)
        p.start()
        self.addCleanup(p.stop)
        self.storage.fd_scope_include_existing = True
        self._activate()
        return self.env["fastegy.drive.migration"].search([("fd_scope_storage_id", "=", self.storage.id),
            ("fd_scope_plan_hash", "=", self.storage._fd_scope_generation())])

    def test_automatic_owned_model_routes_without_manual_model_or_field_selection(self):
        self.assertFalse(self.storage.model_ids)
        self.assertFalse(self.storage.field_ids)
        self._activate()
        self.assertTrue(self.storage.fd_scope_active)
        attachment = self._attachment()
        self.assertTrue(attachment.store_fname.startswith(self.storage.code + "://"))
        self.assertEqual(attachment.raw, b"scope synthetic proof")
        self.assertFalse(attachment.db_datas)
        self.assertFalse(self.storage.model_ids)
        self.assertFalse(self.storage.field_ids)

    def test_shared_model_routes_only_introduced_binary_field(self):
        self._activate(shared=True)
        self.assertFalse(self.storage.get_storage_code_by_model_field("res.partner"))
        self.assertEqual(self.storage.get_storage_code_by_model_field("res.partner", "image_1920"), self.storage.code)
        self.assertFalse(self.storage._fd_scope_lookup("res.company"))
        ordinary = self._attachment()
        self.assertNotIn("://", ordinary.store_fname)

    def test_direct_columns_are_reported_without_mutating_definition(self):
        column = {"name": "fixture_column", "store": True, "attachment": False, "modules": ["mail"], "origin": "mail"}
        self._activate(extra=column)
        line = self.storage.fd_scope_line_ids.filtered(lambda l: l.field_name == "fixture_column")
        self.assertEqual(line.status, "column")
        self.assertFalse(line.route_key)
        self.assertFalse(self.storage._fd_scope_lookup("res.partner", "fixture_column"))

    def test_existing_manual_storage_wins_and_is_reported_as_a_conflict(self):
        other = self._storage()
        other.model_xmlids = "base.model_res_partner"
        self._activate()
        self.assertEqual(self.storage.get_storage_code_by_model_field("res.partner"), other.code)
        self.assertTrue(self.storage.fd_scope_line_ids.filtered(lambda l: l.status == "conflict"))
        self.assertFalse(self.storage.fd_scope_line_ids.filtered("route_key"))

    def test_two_automatic_storages_do_not_claim_the_same_route(self):
        self._activate()
        other = self._storage()
        other.fd_scope_module_ids = self.module
        self._activate(storage=other)
        self.assertFalse(other.fd_scope_line_ids.filtered("route_key"))
        self.assertEqual(self.storage._fd_scope_lookup("res.partner"), self.storage.code)

    def test_pause_preserves_previously_stored_file_readback(self):
        self._activate()
        attachment = self._attachment()
        self.storage.action_fd_scope_pause()
        self.assertFalse(self.storage._fd_scope_lookup("res.partner"))
        self.assertEqual(attachment.raw, b"scope synthetic proof")
        self.assertNotIn("://", self._attachment().store_fname)

    def test_pending_application_selection_is_not_applied_by_background_refresh(self):
        self._activate()
        before = self.storage.fd_scope_applied_modules
        self.storage.fd_scope_module_ids = self.env["ir.module.module"].search([("name", "=", "base")])
        self.assertTrue(self.storage.fd_scope_dirty)
        with patch.object(type(self.storage), "_fd_scope_descriptors", return_value=self._descriptors()):
            self.storage.action_fd_scope_refresh()
        self.assertEqual(self.storage.fd_scope_applied_modules, before)
        self.assertEqual(self.storage._fd_scope_lookup("res.partner"), self.storage.code)

    def test_refresh_adds_new_field_retires_missing_field_and_is_idempotent(self):
        self._activate()
        first_ids = self.storage.fd_scope_line_ids.ids
        with patch.object(type(self.storage), "_fd_scope_descriptors", return_value=self._descriptors()):
            self.storage.action_fd_scope_refresh()
        self.assertEqual(set(self.storage.fd_scope_line_ids.ids), set(first_ids))
        descriptors = self._descriptors()
        descriptors[0]["fields"] = [{"name": "image_1024", "store": True, "attachment": True,
                                     "origin": "mail", "modules": ["mail"]}]
        with patch.object(type(self.storage), "_fd_scope_descriptors", return_value=descriptors):
            self.storage.action_fd_scope_refresh()
        old = self.storage.fd_scope_line_ids.filtered(lambda l: l.field_name == "image_1920")
        self.assertEqual(old.status, "retired")
        self.assertFalse(old.route_key)
        self.assertTrue(self.storage.fd_scope_line_ids.filtered(lambda l: l.field_name == "image_1024" and l.current))

    def test_missing_application_does_not_fall_back_to_a_global_scope(self):
        self._activate()
        self.storage._fd_scope_write({"fd_scope_applied_modules": json.dumps(["missing_fixture_application"])})
        self.storage.action_fd_scope_refresh()
        self.assertFalse(self.storage.fd_scope_line_ids.filtered("route_key"))
        self.assertTrue(self.storage.fd_scope_line_ids.filtered(lambda l: l.status == "missing"))

    def test_discovery_requires_admin_and_verified_original_connection(self):
        with self.assertRaises(AccessError):
            self.storage.with_user(self.env.ref("base.public_user")).action_fd_scope_activate()
        self.storage.fd_readonly = True
        with self.assertRaises(UserError):
            self.storage.action_fd_scope_activate()

    def test_results_are_read_only_and_backfill_is_opt_in(self):
        self._attachment(local=True)
        self._activate()
        self.assertFalse(self.env["fastegy.drive.migration"].search_count([("fd_scope_storage_id", "=", self.storage.id)]))
        with self.assertRaises(ValidationError):
            self.storage.write({"fd_scope_active": True})
        user = self.env["res.users"].create({"name": "Scope settings reader", "login": uuid.uuid4().hex,
            "groups_id": [(6, 0, [self.env.ref("base.group_system").id, self.env.ref("base.group_user").id])]})
        with self.assertRaises(AccessError):
            self.storage.fd_scope_line_ids.with_user(user).write({"route_key": "forged/"})

    def test_backfill_queues_only_synthetic_record_and_verifies_bytes(self):
        attachment = self._attachment(local=True)
        migrations = self._scoped_backfill()
        self.assertEqual(len(migrations), 1)
        migrations._fd_scope_enqueue_batch()
        jobs = self.env["fastegy.drive.job"].search([("migration_id", "in", migrations.ids)])
        self.assertEqual(jobs.attachment_id, attachment)
        jobs.action_run()
        self.assertEqual(jobs.state, "done")
        self.assertTrue(attachment.store_fname.startswith(self.storage.code + "://"))
        self.assertEqual(attachment.raw, b"scope synthetic proof")

    def test_pause_cancels_a_queued_backfill_without_touching_source(self):
        attachment = self._attachment(local=True)
        migrations = self._scoped_backfill()
        migrations._fd_scope_enqueue_batch()
        job = self.env["fastegy.drive.job"].search([("migration_id", "in", migrations.ids)])
        self.storage.action_fd_scope_pause()
        job.action_run()
        self.assertEqual(job.state, "cancelled")
        self.assertNotIn("://", attachment.store_fname)
        self.assertEqual(attachment.raw, b"scope synthetic proof")

    def test_repeated_backfill_activation_does_not_duplicate_a_snapshot(self):
        self._attachment(local=True)
        initial = self._scoped_backfill()
        self._activate()
        self.assertEqual(self.env["fastegy.drive.migration"].search([("fd_scope_storage_id", "=", self.storage.id)]), initial)

    def test_local_routing_rule_excludes_old_file_from_automatic_backfill(self):
        attachment = self._attachment(local=True)
        model = self.env["ir.model"].search([("model", "=", "res.partner")])
        self.env["fastegy.drive.rule"].create({"name": "Keep this fixture local", "active": True,
            "storage_id": self.storage.id, "model_id": model.id, "mode": "local",
            "record_domain": repr([("id", "=", self.partner.id)])})
        migrations = self._scoped_backfill()
        migrations._fd_scope_enqueue_batch()
        self.assertFalse(self.env["fastegy.drive.job"].search_count([("migration_id", "in", migrations.ids)]))
        self.assertEqual(migrations.fd_scope_skipped, 1)
        self.assertEqual(migrations.state, "done")
        self.assertNotIn("://", attachment.store_fname)

    def test_a_new_scope_generation_cancels_previously_queued_work(self):
        attachment = self._attachment(local=True)
        migration = self._scoped_backfill()
        migration._fd_scope_enqueue_batch()
        job = self.env["fastegy.drive.job"].search([("migration_id", "=", migration.id)])
        self.assertEqual(len(job), 1)
        old_generation = self.storage._fd_scope_generation()
        with patch.object(type(self.storage), "_fd_scope_descriptors", return_value=self._descriptors(shared=True)):
            self.storage.action_fd_scope_refresh()
        self.assertNotEqual(old_generation, self.storage._fd_scope_generation())
        job.action_run()
        self.assertEqual(job.state, "cancelled")
        self.assertNotIn("://", attachment.store_fname)

    def test_snapshot_includes_files_created_in_the_same_transaction(self):
        attachment = self._attachment(local=True)
        migration = self._scoped_backfill()
        self.assertEqual(len(migration), 1)
        self.assertGreaterEqual(migration.preview_at, attachment.write_date)
        self.assertIn(attachment, self.env["ir.attachment"].search(migration._domain()))

    def test_transfer_rejects_large_source_before_reading_its_binary(self):
        attachment = self.env["ir.attachment"].with_context(storage_location="file").create({
            "name": "large synthetic fixture.bin", "raw": b"x" * (2 * 1048576),
            "res_model": "res.partner", "res_id": self.partner.id})
        self.storage.fd_max_file_mb = 1
        job = self.env["fastegy.drive.job"]._enqueue(self.storage, attachment)
        with patch.object(type(attachment), "_file_read", side_effect=AssertionError("Large content must not be read")) as read:
            job.action_run()
        read.assert_not_called()
        self.assertEqual(job.state, "failed")
        self.assertNotIn("://", attachment.store_fname)

    def test_live_registry_provenance_scanner_does_not_use_name_prefixes(self):
        descriptors = self.storage._fd_scope_descriptors()
        native = next(m for m in descriptors if m["name"] == "res.partner")
        self.assertEqual(native["owner"], "base")
        self.assertTrue(any(f["name"] == "image_1920" and f["origin"] for f in native["fields"]))
        rows, fingerprint = self.storage._fd_scope_plan(["fastegy_drive_storage"])
        self.assertTrue(fingerprint)
        self.assertTrue(any(r["model_name"] == "fastegy.drive.scope.line" and r["status"] == "excluded" for r in rows))

    def test_transfer_status_distinguishes_preview_from_completed_upload(self):
        self._attachment(local=True)
        migrations = self._scoped_backfill()
        self.storage._compute_fd_transfer_status()
        self.assertIn('لم تُضف ملفاتها إلى الطابور', self.storage.fd_transfer_status)
        self.assertFalse(self.env['fastegy.drive.job'].search_count([('migration_id', 'in', migrations.ids)]))
        migrations._fd_scope_enqueue_batch()
        self.storage._compute_fd_transfer_status()
        self.assertIn('انتظار 1', self.storage.fd_transfer_status)
        self.assertIn('مكتمل 0', self.storage.fd_transfer_status)

    def test_transfer_status_ignores_previous_scope_generation(self):
        self._attachment(local=True)
        migrations = self._scoped_backfill()
        migrations.fd_scope_plan_hash = 'previous-generation'
        self.storage._compute_fd_transfer_status()
        self.assertIn('لا توجد معاينات في الخطة الحالية', self.storage.fd_transfer_status)

    def test_transfer_status_off_does_not_claim_completion(self):
        self.storage._compute_fd_transfer_status()
        self.assertIn('غير مفعّل', self.storage.fd_transfer_status)

    def test_native_scheduler_dispatches_only_installed_operation(self):
        cron = self.env.ref('fastegy_drive_storage.cron_drive_scope')
        action = cron.ir_actions_server_id
        action.write({'state': 'fastegy_drive_maintenance'})
        with patch.object(type(self.storage), '_cron_fd_scope') as run:
            action.run()
            run.assert_called_once()
        duplicate = action.copy({'state': 'fastegy_drive_maintenance'})
        with self.assertRaises(ValidationError):
            duplicate.run()

    def test_native_scheduler_preserves_storage_safe_eval_guard(self):
        from odoo.tools.safe_eval import safe_eval
        with self.assertRaises(Exception) as error:
            safe_eval("storage.write({'name': 'must not change'})", {'storage': self.storage}, mode='exec')
        self.assertIn('not allowed', str(error.exception))
        self.assertEqual(self.storage.name, 'Scope isolated')
