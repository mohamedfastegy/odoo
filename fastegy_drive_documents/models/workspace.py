from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class DriveWorkspace(models.Model):
    _name = "fastegy.drive.workspace"
    _description = "Documents folder Drive storage mapping"
    _sql_constraints = [("one_folder_storage", "unique(folder_id)", "Each Documents folder may have one mapping.")]

    name = fields.Char(related="folder_id.name", readonly=True)
    active = fields.Boolean(default=True)
    folder_id = fields.Many2one("documents.document", required=True, ondelete="cascade", domain=[("type", "=", "folder")])
    storage_id = fields.Many2one("fs.storage", required=True, ondelete="restrict", domain=[("protocol", "=", "fastegy_gdrive")])
    include_subfolders = fields.Boolean(default=True)

    @api.constrains("folder_id", "storage_id")
    def _validate_folder(self):
        for mapping in self:
            if mapping.folder_id.type != "folder":
                raise ValidationError(_("Choose an actual Documents folder."))
            mapping.folder_id.check_access("read")

    def action_queue_existing(self):
        self.env["fs.storage"]._fd_admin()
        self.ensure_one()
        folders = self.folder_id
        if self.include_subfolders:
            frontier = folders
            while frontier:
                frontier = self.env["documents.document"].search([("folder_id", "in", frontier.ids),
                    ("type", "=", "folder"), ("id", "not in", folders.ids)])
                folders |= frontier
        documents = self.env["documents.document"].search([("folder_id", "in", folders.ids), ("type", "=", "binary")])
        for document in documents:
            document.check_access("read")
            if document._fd_workspace_storage() != self.storage_id:
                continue
            attachment = document.attachment_id
            if attachment and not (attachment.store_fname or "").startswith(self.storage_id.code + "://"):
                self.env["fastegy.drive.job"]._enqueue(self.storage_id, attachment)
        return {"type": "ir.actions.act_window", "name": _("Workspace transfer tasks"),
            "res_model": "fastegy.drive.job", "view_mode": "list,form", "domain": [("storage_id", "=", self.storage_id.id)]}


class DocumentsDocument(models.Model):
    _inherit = "documents.document"

    def _fd_workspace_storage(self):
        self.ensure_one()
        folder, seen, depth = self.folder_id, set(), 0
        while folder and folder.id not in seen:
            seen.add(folder.id)
            mapping = self.env["fastegy.drive.workspace"].sudo().search([("folder_id", "=", folder.id)], limit=1)
            if mapping and (not depth or mapping.include_subfolders):
                return mapping.storage_id
            folder, depth = folder.folder_id, depth + 1
        return self.env["fs.storage"]

    def _fd_route_workspace(self):
        if self.env.context.get("fd_skip_workspace"):
            return
        for document in self:
            if document.type != "binary" or not document.attachment_id:
                continue
            storage = document._fd_workspace_storage()
            attachment = document.attachment_id
            if storage and not (attachment.store_fname or "").startswith(storage.code + "://"):
                # لا نغير صلاحيات المستند أو مشاركته عند تغيير مكان المحتوى.
                data = attachment.with_context(bin_size=False).raw
                attachment.with_context(storage_location=storage.code, fd_skip_workspace=True).write({"raw": data})
                if attachment.raw != data:
                    raise ValidationError(_("Workspace storage verification failed."))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._fd_route_workspace()
        return records

    def write(self, values):
        result = super().write(values)
        if {"folder_id", "attachment_id", "datas"}.intersection(values):
            self._fd_route_workspace()
        return result
