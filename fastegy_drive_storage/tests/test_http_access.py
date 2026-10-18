import uuid
from unittest.mock import patch

import fsspec
from odoo import fields
from odoo.http import request
from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install", "fastegy_drive_trial")
class TestDriveHTTPAccess(HttpCase):
    def setUp(self):
        super().setUp()
        self.storage = self.env["fs.storage"].create({"name": "HTTP isolated test", "code": "fd_http_" + uuid.uuid4().hex,
            "protocol": "fastegy_gdrive", "fd_client_id": "test", "fd_client_secret": "test", "fd_refresh_token": "test",
            "fd_root_folder_id": "offline", "fd_readonly": False, "fd_connection_checked": True, "fd_allow_writes": True})
        original, storage_id = type(self.storage)._get_filesystem, self.storage.id
        remote = fsspec.filesystem("memory", skip_instance_cache=True)
        def filesystem(storage):
            return remote if storage.id == storage_id else original(storage)
        patcher = patch.object(type(self.storage), "_get_filesystem", filesystem)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.recipient = self.env["res.users"].create({"name": "Drive portal HTTP", "login": "fd_http_" + uuid.uuid4().hex,
            "password": "fd-http-isolated-test", "groups_id": [(6, 0, [self.env.ref("base.group_portal").id])]})
        self.other = self.env["res.users"].create({"name": "Other portal HTTP", "login": "fd_other_" + uuid.uuid4().hex,
            "password": "fd-http-isolated-test", "groups_id": [(6, 0, [self.env.ref("base.group_portal").id])]})
        self.employee = self.env["res.users"].create({"name": "Internal Drive HTTP", "login": "fd_staff_" + uuid.uuid4().hex,
            "password": "fd-http-isolated-test", "groups_id": [(6, 0, [self.env.ref("base.group_user").id])]})
        self.attachment = self.env["ir.attachment"].with_context(storage_location=self.storage.code).create({
            "name": "HTTP proof.txt", "raw": b"http proof", "res_model": "res.partner", "res_id": self.env.user.partner_id.id})
        self.share = self.env["fastegy.drive.share"].create({"attachment_id": self.attachment.id,
            "partner_id": self.recipient.partner_id.id})
        # نفحص بوابة التفويض الحقيقية؛ النقل الخارجي له اختبارات مستقلة.
        streamer = patch("odoo.addons.fastegy_drive_storage.controllers.files.stream_blob",
                         lambda blob: request.make_response(b"authorized file content"))
        streamer.start()
        self.addCleanup(streamer.stop)

    def test_recipient_can_download_only_while_share_is_active(self):
        self.authenticate(self.recipient.login, "fd-http-isolated-test")
        self.assertEqual(self.url_open(self.share.url).status_code, 200)
        self.share.action_revoke()
        self.assertEqual(self.url_open(self.share.url).status_code, 404)

    def test_another_portal_user_cannot_guess_share_or_manifest(self):
        self.authenticate(self.other.login, "fd-http-isolated-test")
        self.assertEqual(self.url_open(self.share.url).status_code, 404)
        self.assertEqual(self.url_open("/fastegy_drive_storage/manifest/%s" % self.storage.id).status_code, 403)

    def test_regular_download_retains_native_attachment_access(self):
        self.authenticate(self.other.login, "fd-http-isolated-test")
        response = self.url_open("/fastegy_drive_storage/download/%s" % self.attachment.id)
        self.assertIn(response.status_code, (403, 404))

    def test_regular_download_allows_an_authorized_internal_reader(self):
        self.attachment.write({"res_id": self.employee.partner_id.id})
        self.attachment.with_user(self.employee).check("read")
        self.authenticate(self.employee.login, "fd-http-isolated-test")
        response = self.url_open("/fastegy_drive_storage/download/%s" % self.attachment.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"authorized file content")
