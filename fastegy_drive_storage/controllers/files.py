import html
import json
import re

from werkzeug.exceptions import Forbidden, NotFound, RequestedRangeNotSatisfiable

from odoo import fields, http
from odoo.http import request, content_disposition


def require_admin():
    if not request.env.user.has_group("base.group_system"):
        raise Forbidden()


def json_download(value, filename):
    return request.make_response(json.dumps(value, ensure_ascii=False, indent=2), headers=[
        ("Content-Type", "application/json; charset=utf-8"), ("Content-Disposition", content_disposition(filename)),
        ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")])


def stream_blob(blob):
    client = blob.storage_id._fd_client()
    remote = client.metadata(blob.file_id)
    client.file_path(remote)
    if blob.md5 and remote.get("md5Checksum") != blob.md5:
        raise NotFound("Stored file integrity needs review")
    size = int(remote["size"])
    headers = [("Content-Type", blob.mimetype or "application/octet-stream"),
        ("Content-Disposition", content_disposition(blob.name)), ("Cache-Control", "private, no-store"),
        ("X-Content-Type-Options", "nosniff"), ("Accept-Ranges", "bytes")]
    requested = request.httprequest.headers.get("Range")
    if requested:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
        if not match or not any(match.groups()) or not size:
            raise RequestedRangeNotSatisfiable(length=size)
        start, end = match.groups()
        if start:
            start, end = int(start), min(int(end) if end else size-1, size-1)
        else:
            start, end = max(0, size-int(end)), size-1
        if start > end or start >= size:
            raise RequestedRangeNotSatisfiable(length=size)
        headers += [("Content-Range", "bytes %s-%s/%s" % (start, end, size)), ("Content-Length", str(end-start+1))]
        response = request.make_response(client.iter_range(remote, start, end), headers=headers)
        response.status_code = 206
        return response
    headers.append(("Content-Length", str(size)))
    return request.make_response(client.iter_download(remote), headers=headers)


class DriveFiles(http.Controller):
    @http.route("/fastegy_drive_storage/download/<int:attachment_id>", type="http", auth="user", methods=["GET"])
    def download(self, attachment_id, **kwargs):
        attachment = request.env["ir.attachment"].browse(attachment_id).exists()
        if not attachment:
            raise NotFound()
        attachment.check_access("read")
        attachment.check("read")
        blob = request.env["fastegy.drive.blob"].sudo().search([
            ("attachment_id", "=", attachment.id), ("state", "=", "active"),
            ("path", "=", (attachment.store_fname or "").partition("://")[2])], limit=1)
        if not blob:
            return request.redirect("/web/content/%s?download=true" % attachment.id)
        return stream_blob(blob)

    @http.route("/fastegy_drive_storage/blob/<int:blob_id>", type="http", auth="user", methods=["GET"])
    def blob_download(self, blob_id, **kwargs):
        require_admin()
        blob = request.env["fastegy.drive.blob"].browse(blob_id).exists()
        if not blob:
            raise NotFound()
        return stream_blob(blob)

    @http.route("/fastegy_drive_storage/manifest/<int:storage_id>", type="http", auth="user", methods=["GET"])
    def manifest(self, storage_id, **kwargs):
        require_admin()
        storage = request.env["fs.storage"].browse(storage_id).exists()
        if not storage:
            raise NotFound()
        fields_to_export = ["name", "path", "file_id", "attachment_id", "res_model", "res_id", "res_field", "mimetype",
            "size", "checksum", "md5", "state", "orphan_since", "checked_at"]
        rows = request.env["fastegy.drive.blob"].search([("storage_id", "=", storage.id)]).read(fields_to_export)
        for row in rows:
            row.pop("id", None)
            row["attachment_id"] = row["attachment_id"][0] if row.get("attachment_id") else False
            for key, value in row.items():
                if hasattr(value, "isoformat"):
                    row[key] = value.isoformat()
        return json_download({"schema": 1, "storage_code": storage.code, "namespace": storage.fd_namespace,
            "root_folder_id": storage.fd_root_folder_id, "shared_drive_id": storage.fd_shared_drive_id,
            "exported_at": fields.Datetime.to_string(fields.Datetime.now()), "files": rows}, "drive-recovery-manifest.json")

    @http.route("/fastegy_drive_storage/coverage/<int:audit_id>", type="http", auth="user", methods=["GET"])
    def coverage(self, audit_id, **kwargs):
        require_admin()
        audit = request.env["fastegy.drive.coverage"].browse(audit_id).exists()
        if not audit:
            raise NotFound()
        return json_download({"scanned_at": fields.Datetime.to_string(audit.scanned_at), "note": audit.explanation,
            "fields": audit.line_ids.read(["model_name", "field_name", "status", "storage_code"])}, "file-coverage.json")

    @http.route("/my/fastegy-drive", type="http", auth="user", methods=["GET"], website=True)
    def portal_list(self, **kwargs):
        shares = request.env["fastegy.drive.share"].sudo().search([
            ("partner_id", "=", request.env.user.partner_id.id), ("expires_at", ">", fields.Datetime.now())])
        return request.render("fastegy_drive_storage.portal_drive_files", {"shares": shares})

    @http.route("/my/fastegy-drive/<int:share_id>", type="http", auth="user", methods=["GET"])
    def portal_download(self, share_id, **kwargs):
        share = request.env["fastegy.drive.share"].sudo().browse(share_id).exists()
        if (not share or not share.active or share.expires_at <= fields.Datetime.now()
                or share.partner_id != request.env.user.partner_id or share.attachment_id.public):
            raise NotFound()
        attachment = share.attachment_id
        blob = request.env["fastegy.drive.blob"].sudo().search([("attachment_id", "=", attachment.id),
            ("path", "=", (attachment.store_fname or "").partition("://")[2]), ("state", "=", "active")], limit=1)
        if not blob:
            raise NotFound()
        return stream_blob(blob)

    @http.route("/fastegy_drive_storage/picker/<int:wizard_id>", type="http", auth="user", methods=["GET"])
    def picker(self, wizard_id, **kwargs):
        require_admin()
        wizard = request.env["fastegy.drive.import"].browse(wizard_id).exists()
        if not wizard:
            raise NotFound()
        client = wizard.storage_id._fd_client()
        client._refresh()
        config = {"token": client._token, "key": wizard.storage_id.fd_picker_api_key,
            "app": wizard.storage_id.fd_picker_app_id, "origin": request.httprequest.host_url.rstrip("/")}
        encoded = json.dumps(config).replace("<", "\\u003c").replace("&", "\\u0026")
        page = """<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><title>اختيار ملف من درايف</title>
<p>اختر ملفًا لربطه بالتطبيق، ثم ارجع إلى نافذة الاستيراد لمراجعة الوجهة وتنفيذ النسخ.</p>
<button onclick="openPicker()">اختيار ملف من جوجل درايف</button>
<form id="selection" method="post" action="/fastegy_drive_storage/picker/selection">
<input type="hidden" name="csrf_token" value="%s"><input type="hidden" name="wizard_id" value="%s">
<input type="hidden" id="file" name="file_id"></form>
<script>const c=%s;function openPicker(){gapi.load('picker',()=>{const p=new google.picker.PickerBuilder()
.addView(google.picker.ViewId.DOCS).setOAuthToken(c.token).setDeveloperKey(c.key).setAppId(c.app)
.setOrigin(c.origin).setCallback(d=>{if(d.action===google.picker.Action.PICKED){
document.getElementById('file').value=d.docs[0].id;document.getElementById('selection').submit();}}).build();p.setVisible(true);});}</script>
<script src="https://apis.google.com/js/api.js"></script></html>""" % (html.escape(request.csrf_token()), wizard.id, encoded)
        return request.make_response(page, headers=[("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "SAMEORIGIN")])

    @http.route("/fastegy_drive_storage/picker/selection", type="http", auth="user", methods=["POST"], csrf=True)
    def picker_selection(self, wizard_id=None, file_id=None, **kwargs):
        require_admin()
        if not str(wizard_id).isdigit() or not re.fullmatch(r"[A-Za-z0-9_-]+", file_id or ""):
            raise NotFound()
        wizard = request.env["fastegy.drive.import"].browse(int(wizard_id)).exists()
        if not wizard:
            raise NotFound()
        wizard.drive_file_id = file_id
        action = request.env.ref("fastegy_drive_storage.action_drive_import").id
        return request.redirect("/web#action=%s&model=fastegy.drive.import&id=%s&view_type=form" % (action, wizard.id))
