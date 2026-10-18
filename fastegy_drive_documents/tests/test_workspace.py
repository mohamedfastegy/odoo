import uuid
from unittest.mock import patch

import fsspec
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "fastegy_drive_trial")
class TestDriveWorkspaces(TransactionCase):
    def setUp(self):
        super().setUp()
        self.storage = self.env["fs.storage"].create({"name": "Workspace test", "code": "fd_docs_" + uuid.uuid4().hex,
            "protocol": "fastegy_gdrive", "fd_client_id": "test", "fd_client_secret": "test",
            "fd_refresh_token": "test", "fd_root_folder_id": "offline_folder", "fd_readonly": False,
            "fd_connection_checked": True, "fd_allow_writes": True})
        original, storage_id = type(self.storage)._get_filesystem, self.storage.id
        remote = fsspec.filesystem("memory", skip_instance_cache=True)
        def filesystem(storage):
            return remote if storage.id == storage_id else original(storage)
        patcher = patch.object(type(self.storage), "_get_filesystem", filesystem)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_document_follows_folder_without_public_sharing(self):
        folder = self.env["documents.document"].create({"name": "Drive workspace trial", "type": "folder"})
        self.env["fastegy.drive.workspace"].create({"folder_id": folder.id, "storage_id": self.storage.id})
        attachment = self.env["ir.attachment"].with_context(storage_location="file").create({"name": "proof.txt", "raw": b"workspace proof"})
        document = self.env["documents.document"].create({"name": "proof.txt", "type": "binary",
            "folder_id": folder.id, "attachment_id": attachment.id})
        self.assertTrue(document.attachment_id.store_fname.startswith(self.storage.code + "://"))
        self.assertEqual(document.attachment_id.raw, b"workspace proof")
        self.assertFalse(document.attachment_id.public)

    def test_nearest_folder_rule_wins(self):
        folder = self.env["documents.document"].create({"name": "Parent drive folder", "type": "folder"})
        child = self.env["documents.document"].create({"name": "Child", "type": "folder", "folder_id": folder.id})
        mapping = self.env["fastegy.drive.workspace"].create({"folder_id": folder.id, "storage_id": self.storage.id})
        document = self.env["documents.document"].create({"name": "Placeholder", "type": "url", "url": "https://example.com", "folder_id": child.id})
        self.assertEqual(document._fd_workspace_storage(), self.storage)
        mapping.include_subfolders = False
        self.assertFalse(document._fd_workspace_storage())
