import uuid
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'fastegy_drive_trial')
class TestBusinessPresentation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.storage = self.env['fs.storage'].create({
            'name': 'Business folder isolated test', 'code': 'folder_' + uuid.uuid4().hex,
            'protocol': 'fastegy_gdrive', 'fd_folder_layout': 'business'})
        self.partner = self.env['res.partner'].with_context(mail_create_nosubscribe=True).create({'name': 'Business test record'})
        self.attachment = self.env['ir.attachment'].with_context(storage_location='file').create({
            'name': 'isolated.txt', 'raw': b'isolated business presentation test',
            'res_model': 'res.partner', 'res_id': self.partner.id, 'mimetype': 'text/plain'})

    def test_orm_adapter_keeps_stable_record_keys_on_rename(self):
        before = self.attachment._fd_business_folders(self.storage)
        self.partner.name = 'Renamed isolated record'
        after = self.attachment._fd_business_folders(self.storage)
        self.assertEqual(before[2]['key'], after[2]['key'])
        self.assertNotEqual(before[2]['name'], after[2]['name'])
        self.assertEqual(after[-1]['key'], 'business:v1:category:documents')

    def test_chatter_attachment_is_grouped_by_actual_message_link(self):
        self.env['mail.message'].create({'model': 'res.partner', 'res_id': self.partner.id,
            'message_type': 'comment', 'body': 'Isolated internal folder test',
            'attachment_ids': [(4, self.attachment.id)]})
        folders = self.attachment._fd_business_folders(self.storage)
        self.assertEqual(folders[-1]['key'], 'business:v1:category:chatter')

    def test_metadata_queue_reuses_pending_job_without_touching_bytes(self):
        jobs = self.env['fastegy.drive.job']
        before = self.attachment.raw
        first = jobs._enqueue(self.storage, self.attachment, 'metadata')
        second = jobs._enqueue(self.storage, self.attachment, 'metadata')
        self.assertEqual(first, second)
        self.assertEqual(self.attachment.raw, before)
        self.assertEqual(first.kind, 'metadata')
