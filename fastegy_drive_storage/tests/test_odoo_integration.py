"""Run with an Odoo 18 test database; these are not the standalone offline tests."""
import base64
import hashlib
import uuid
import os
from datetime import timedelta
from unittest.mock import Mock, patch

import fsspec

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError
from odoo import fields
from cryptography.fernet import Fernet

from ..hooks import uninstall_hook


@tagged("post_install", "-at_install", "fastegy_drive_trial")
class TestDriveStorageIntegration(TransactionCase):
    def setUp(self):
        super().setUp()
        self.storage = self.env["fs.storage"].create({
            "name": "Drive integration test", "code": "fd_test_" + uuid.uuid4().hex,
            "protocol": "fastegy_gdrive", "fd_client_id": "offline-client",
            "fd_client_secret": "offline-secret", "fd_refresh_token": "offline-token",
            "fd_root_folder_id": "offline-folder", "fd_readonly": False,
            "fd_connection_checked": True, "fd_allow_writes": True,
            "model_xmlids": "base.model_res_partner",
        })
        # يتولى موصل درايف الحقيقي اختبارات مستقلة بمحاكاة الشبكة.
        # هنا نختبر تكامل حقول أودو والترحيل بعزل الشبكة تمامًا.
        self.remote = fsspec.filesystem("memory", skip_instance_cache=True)
        original = type(self.storage)._get_filesystem
        code = self.storage.code
        remote = self.remote

        def filesystem(record):
            if record.code == code:
                return remote
            return original(record)

        self.patcher = patch.object(type(self.storage), "_get_filesystem", filesystem)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.partner = self.env["res.partner"].create({"name": "Drive test record"})

    def _attachment(self, data=b"original bytes", local=False, partner=None):
        model = self.env["ir.attachment"]
        if local:
            model = model.with_context(storage_location="file")
        return model.create({"name": "sample.bin", "raw": data,
                             "res_model": "res.partner", "res_id": (partner or self.partner).id})

    def test_drive_protocol_is_available_in_form_metadata(self):
        metadata = self.env["fs.storage"].fields_get(["protocol", "options_protocol"])
        for field in ("protocol", "options_protocol"):
            self.assertIn("fastegy_gdrive", dict(metadata[field]["selection"]))

    def test_registry_tracks_replacements_as_versions(self):
        attachment = self._attachment()
        initial = attachment.store_fname
        attachment.write({"raw": b"replacement bytes"})
        blobs = self.env["fastegy.drive.blob"].search([("attachment_id", "=", attachment.id)])
        self.assertEqual(len(blobs), 2)
        old = blobs.filtered(lambda b: b.path == initial.partition("://")[2])
        self.assertEqual(old.state, "orphan")
        self.assertTrue(old.orphan_since)
        self.assertEqual(len(blobs.filtered(lambda b: b.state == "active")), 1)

    def test_reference_adoption_preserves_verified_storage_metadata_without_copy(self):
        for data in (b"referenced existing bytes", b""):
            with self.subTest(bytes=len(data)):
                path = uuid.uuid4().hex
                self.remote.pipe(path, data)
                attachment = self.env["ir.attachment"].with_context(storage_location=self.storage.code).create({
                    "name": "Existing reference.txt", "mimetype": "text/plain",
                    "res_model": "res.partner", "res_id": self.partner.id})
                client = Mock(readonly=False)
                client.lookup.return_value = {"id": "shortcut", "shortcutDetails": {"targetId": "source"},
                    "size": str(len(data)), "md5Checksum": hashlib.md5(data).hexdigest()}
                client.file_path.return_value = path
                before = set(self.remote.find(""))
                with patch.object(type(self.storage), "_fd_client", return_value=client):
                    attachment._fd_adopt_verified_reference(self.storage, path, data)
                self.assertEqual(attachment.store_fname, self.storage.code + "://" + path)
                self.assertEqual(attachment.checksum, hashlib.sha1(data).hexdigest())
                self.assertEqual(attachment.file_size, len(data))
                self.assertEqual(attachment.raw, data)
                self.assertFalse(attachment.db_datas)
                self.assertEqual(set(self.remote.find("")), before)
                self.assertTrue(self.env["fastegy.drive.blob"].search_count([("attachment_id", "=", attachment.id)]))

    def test_reference_adoption_requires_the_calling_user_to_be_an_admin(self):
        attachment = self._attachment(local=True)
        with self.assertRaises(AccessError):
            attachment.with_user(self.env.ref("base.public_user"))._fd_adopt_verified_reference(
                self.storage.sudo(), uuid.uuid4().hex, b"bytes")

    def test_trash_rejects_any_live_reference(self):
        attachment = self._attachment()
        blob = self.env["fastegy.drive.blob"].search([("attachment_id", "=", attachment.id)])
        blob.write({"state": "orphan", "orphan_since": fields.Datetime.now() - timedelta(days=100)})
        with self.assertRaises(UserError):
            blob.action_trash()

    def test_dashboard_counts_current_versions_and_failed_tasks(self):
        first = self._attachment(b"old")
        first.write({"raw": b"12345"})
        second = self._attachment(b"1234567")
        self.env["fastegy.drive.job"]._enqueue(self.storage, second).write({"state": "failed"})
        self.assertEqual(self.storage.fd_blob_count, 2)
        self.assertEqual(self.storage.fd_blob_bytes, 12)
        self.assertEqual(self.storage.fd_orphan_count, 1)
        self.assertEqual(self.storage.fd_failed_jobs, 1)

    def test_retry_limit_is_bounded(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["fastegy.drive.job"].create({"storage_id": self.storage.id, "max_attempts": 0})

    def test_health_alert_is_an_internal_activity_and_is_not_duplicated(self):
        self.storage.fd_notify_user_id = self.env.user
        with patch.object(type(self.storage), "_fd_client", side_effect=PermissionError("offline permission")):
            self.storage.action_fd_health()
            self.storage.action_fd_health()
        activities = self.env["mail.activity"].search([
            ("res_model", "=", "fs.storage"), ("res_id", "=", self.storage.id),
            ("summary", "=", "Drive storage needs attention")])
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities.user_id, self.env.user)

    def test_unlink_leaves_recoverable_registry_entry(self):
        attachment = self._attachment()
        blob = self.env["fastegy.drive.blob"].search([("attachment_id", "=", attachment.id)])
        attachment.unlink()
        self.assertFalse(blob.attachment_id)
        self.assertEqual(blob.state, "orphan")

    def test_queue_failure_does_not_stop_another_file(self):
        bad = self._attachment(b"bad job", local=True)
        good = self._attachment(b"good job", local=True)
        jobs = self.env["fastegy.drive.job"]
        first, second = jobs._enqueue(self.storage, bad), jobs._enqueue(self.storage, good)
        original = type(bad)._storage_file_write
        bad_id = bad.id
        def fail_one(record, data):
            if record.id == bad_id:
                raise OSError("offline transport")
            return original(record, data)
        with patch.object(type(bad), "_storage_file_write", fail_one):
            (first | second).action_run()
        self.assertEqual(first.state, "retry")
        self.assertEqual(second.state, "done")
        self.assertNotIn("://", bad.store_fname)
        self.assertTrue(good.store_fname.startswith(self.storage.code + "://"))
        first.action_retry()
        first.action_run()
        self.assertEqual(first.state, "done")

    def test_queue_is_idempotent_and_detects_changed_source(self):
        attachment = self._attachment(local=True)
        job = self.env["fastegy.drive.job"]._enqueue(self.storage, attachment)
        self.assertEqual(job, self.env["fastegy.drive.job"]._enqueue(self.storage, attachment))
        attachment.with_context(storage_location="file").write({"raw": b"new source"})
        job.action_run()
        self.assertEqual(job.state, "failed")
        self.assertEqual(attachment.raw, b"new source")
        self.assertNotIn("://", attachment.store_fname)

    def test_rule_preview_and_size_and_company_scoping(self):
        rule = self.env["fastegy.drive.rule"].create({"name": "Test image route", "storage_id": self.storage.id,
            "model_id": self.env["ir.model"]._get_id("res.partner"), "company_id": self.env.company.id,
            "mime_patterns": "image/*", "minimum_bytes": 10, "maximum_bytes": 100,
            "test_record_id": self.partner.id, "test_mimetype": "image/jpeg", "test_size": 20})
        rule.action_test_rule()
        self.assertEqual(rule.test_result, self.storage.code)
        rule.test_size = 500
        rule.action_test_rule()
        self.assertNotEqual(rule.test_result, self.storage.code)

    def test_explicit_local_rule_overrides_model_default(self):
        self.env["fastegy.drive.rule"].create({"name": "Keep PDF local", "active": True, "storage_id": self.storage.id,
            "model_id": self.env["ir.model"]._get_id("res.partner"), "mime_patterns": "application/pdf", "mode": "local"})
        attachment = self.env["ir.attachment"].create({"name": "proof.pdf", "mimetype": "application/pdf",
            "raw": b"%PDF-1.4 test", "res_model": "res.partner", "res_id": self.partner.id})
        self.assertNotIn("://", attachment.store_fname)

    def test_credentials_are_encrypted_and_runtime_decrypts_them(self):
        self.storage.fd_allow_writes = False
        self.storage.fd_readonly = True
        with patch.dict(os.environ, {"FASTEGY_DRIVE_SECRET_KEYS": Fernet.generate_key().decode()}):
            self.storage.write({"fd_client_secret": "offline-secret", "fd_refresh_token": "offline-token"})
            self.storage.action_fd_encrypt_credentials()
            self.assertTrue(self.storage.fd_refresh_token.startswith("fernet:v1:"))
            self.assertNotEqual(self.storage.fd_client_secret, "offline-secret")
            options = self.storage._get_fs_options()
            self.assertEqual(options["client_secret"], "offline-secret")
            self.assertEqual(options["refresh_token"], "offline-token")

    def test_non_admin_cannot_read_operational_registry(self):
        user = self.env["res.users"].create({"name": "Drive restricted operator", "login": "fd_" + uuid.uuid4().hex,
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])]})
        with self.assertRaises(AccessError):
            self.env["fastegy.drive.blob"].with_user(user).search([])
        with self.assertRaises(AccessError):
            self.storage.with_user(user).action_fd_health()

    def test_coverage_reports_actual_runtime_fields(self):
        audit = self.env["fastegy.drive.coverage"].create({"model_ids": [(6, 0, [self.env["ir.model"]._get_id("res.partner")])]})
        audit.action_scan()
        image = audit.line_ids.filtered(lambda l: l.field_name == "image_1920")
        self.assertEqual(len(image), 1)
        self.assertEqual(image.status, "drive")
        self.assertTrue(audit.scanned_at)

    def test_private_share_is_exact_recipient_and_revocable(self):
        attachment = self._attachment()
        user = self.env["res.users"].create({"name": "Portal file recipient", "login": "fd_portal_" + uuid.uuid4().hex,
            "groups_id": [(6, 0, [self.env.ref("base.group_portal").id])]})
        share = self.env["fastegy.drive.share"].create({"attachment_id": attachment.id, "partner_id": user.partner_id.id})
        self.assertFalse(attachment.public)
        self.assertEqual(share.url, "/my/fastegy-drive/%s" % share.id)
        share.action_revoke()
        self.assertFalse(share.active)

    def _draft_attachment(self, content=b"draft upload"):
        return self.env["ir.attachment"].with_context(storage_location="file").create({
            "name": "draft.txt", "raw": content,
            "res_model": "mail.compose.message", "res_id": 0,
        }).with_context(storage_location=None)

    def test_composer_reassignment_moves_local_binary_to_drive(self):
        attachment = self._draft_attachment()
        self.assertNotIn("://", attachment.store_fname)
        attachment.write({"res_model": "res.partner", "res_id": self.partner.id})
        self.assertTrue(attachment.store_fname.startswith(self.storage.code + "://"))
        self.assertFalse(attachment.db_datas)
        self.assertFalse(attachment.index_content)
        self.assertEqual(attachment._file_read(attachment.store_fname), b"draft upload")

    def test_failed_composer_reassignment_restores_owner_and_content(self):
        attachment = self._draft_attachment()
        original = attachment.store_fname
        with patch.object(type(attachment), "_storage_file_write", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                attachment.write({"res_model": "res.partner", "res_id": self.partner.id})
        attachment.invalidate_recordset()
        self.assertEqual(attachment.res_model, "mail.compose.message")
        self.assertEqual(attachment.store_fname, original)
        self.assertEqual(attachment.raw, b"draft upload")

    def test_explicit_local_restore_and_unmapped_owner_are_respected(self):
        attachment = self._draft_attachment()
        original = attachment.store_fname
        attachment.write({"res_model": "res.company", "res_id": self.env.company.id})
        self.assertEqual(attachment.store_fname, original)
        attachment.with_context(storage_location="file").write({
            "res_model": "res.partner", "res_id": self.partner.id,
        })
        self.assertEqual(attachment.store_fname, original)

    def test_corrupt_composer_readback_rolls_back_reassignment(self):
        attachment = self._draft_attachment()
        original = attachment.store_fname
        original_read = type(attachment)._file_read

        def corrupt(record, fname):
            if fname.startswith(self.storage.code + "://"):
                return b"corrupt readback"
            return original_read(record, fname)

        with patch.object(type(attachment), "_file_read", corrupt):
            with self.assertRaises(UserError):
                attachment.write({"res_model": "res.partner", "res_id": self.partner.id})
        attachment.invalidate_recordset()
        self.assertEqual(attachment.res_model, "mail.compose.message")
        self.assertEqual(attachment.store_fname, original)
        self.assertEqual(attachment.raw, b"draft upload")

    def test_native_binary_attachment_and_private_image(self):
        attachment = self._attachment()
        self.assertEqual(attachment.raw, b"original bytes")
        self.assertFalse(attachment.db_datas)
        self.assertFalse(attachment.index_content)
        self.assertTrue(attachment.store_fname.startswith(self.storage.code + "://"))
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg==")
        self.partner.image_1920 = base64.b64encode(png)
        image_attachment = self.env["ir.attachment"].sudo().search([
            ("res_model", "=", "res.partner"), ("res_id", "=", self.partner.id),
            ("res_field", "=", "image_1920")], limit=1)
        self.assertTrue(image_attachment)
        self.assertTrue(image_attachment.store_fname.startswith(self.storage.code + "://"))
        self.assertEqual(base64.b64decode(self.partner.image_1920), image_attachment.raw)

    def test_replacement_and_rollback_preserve_previous_version(self):
        attachment = self._attachment()
        first = attachment.store_fname
        with self.assertRaises(RuntimeError):
            with self.env.cr.savepoint():
                attachment.write({"raw": b"replacement"})
                self.assertNotEqual(first, attachment.store_fname)
                raise RuntimeError("test rollback")
        attachment.invalidate_recordset()
        self.assertEqual(first, attachment.store_fname)
        self.assertEqual(attachment.raw, b"original bytes")

    def test_preview_scoped_migration_and_reverse(self):
        attachment = self._attachment(local=True)
        other_partner = self.env["res.partner"].create({"name": "Outside scope"})
        outside = self._attachment(local=True, partner=other_partner)
        local_path = attachment.store_fname
        model = self.env["ir.model"]._get("res.partner")
        job = self.env["fastegy.drive.migration"].create({
            "name": "One record", "storage_id": self.storage.id,
            "model_id": model.id, "record_id": self.partner.id, "batch_size": 1,
        })
        job.action_preview()
        self.assertEqual(job.preview_count, 1)
        self.assertEqual(attachment.store_fname, local_path)
        job.action_run_batch()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.moved_count, 1)
        self.assertEqual(attachment.raw, b"original bytes")
        self.assertNotIn("://", outside.store_fname)
        reverse = self.env["fastegy.drive.migration"].create({
            "name": "Restore", "storage_id": self.storage.id, "direction": "to_local",
            "model_id": model.id, "record_id": self.partner.id,
        })
        reverse.action_preview()
        reverse.action_run_batch()
        self.assertEqual(reverse.state, "done")
        self.assertNotIn("://", attachment.store_fname)
        self.assertEqual(attachment.raw, b"original bytes")

    def test_failed_destination_does_not_replace_source(self):
        attachment = self._attachment(local=True)
        original_path = attachment.store_fname
        job = self.env["fastegy.drive.migration"].create({
            "storage_id": self.storage.id, "model_id": self.env["ir.model"]._get("res.partner").id,
            "record_id": self.partner.id,
        })
        job.action_preview()
        with patch.object(type(attachment), "_storage_file_write", side_effect=OSError("offline")):
            job.action_run_batch()
        attachment.invalidate_recordset()
        self.assertEqual(job.state, "error")
        self.assertEqual(attachment.store_fname, original_path)
        self.assertEqual(attachment.raw, b"original bytes")
        self.assertEqual(job.moved_count, 0)

    def test_file_preview_excludes_binary_fields_unless_requested(self):
        ordinary = self._attachment(local=True)
        image = self.env["ir.attachment"].with_context(storage_location="file").create({
            "name": "field-image.bin", "raw": b"field payload",
            "res_model": "res.partner", "res_id": self.partner.id, "res_field": "image_128",
        })
        values = {"storage_id": self.storage.id,
                  "model_id": self.env["ir.model"]._get("res.partner").id,
                  "record_id": self.partner.id}
        ordinary_job = self.env["fastegy.drive.migration"].create(values)
        ordinary_job.action_preview()
        self.assertEqual(ordinary_job.preview_count, 1)
        self.assertEqual(self.env["ir.attachment"].sudo().search(ordinary_job._domain()), ordinary)
        all_job = self.env["fastegy.drive.migration"].create(dict(values, include_field_attachments=True))
        all_job.action_preview()
        self.assertEqual(all_job.preview_count, 2)
        self.assertIn(image, self.env["ir.attachment"].sudo().search(all_job._domain()))

    def test_clone_readonly_and_global_default_guard(self):
        self.storage.write({"fd_allow_writes": False, "fd_bound_database": "another-database"})
        self.assertTrue(self.storage._get_fs_options()["readonly"])
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.storage.use_as_default_for_attachments = True

    def test_migration_actions_require_admin(self):
        user = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Drive ordinary user", "login": "drive-" + uuid.uuid4().hex,
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        job = self.env["fastegy.drive.migration"].create({
            "storage_id": self.storage.id, "model_id": self.env["ir.model"]._get("res.partner").id,
        })
        with self.assertRaises(AccessError):
            job.with_user(user).action_preview()

    def test_referenced_storage_cannot_be_deleted_or_uninstalled(self):
        self._attachment()
        with self.assertRaises(UserError):
            self.storage.unlink()
        with self.assertRaises(UserError):
            uninstall_hook(self.env)

