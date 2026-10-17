import secrets
import time

import requests
from werkzeug.exceptions import BadRequest, Forbidden

from odoo import http
from odoo.http import request


class DriveOAuth(http.Controller):
    @http.route("/fastegy_drive_storage/oauth/callback", type="http", auth="user", methods=["GET"])
    def callback(self, state=None, code=None, error=None, **kwargs):
        if not request.env.user.has_group("base.group_system"):
            raise Forbidden()
        pending = request.session.pop("fastegy_drive_oauth", {})
        expected = pending.get("state", "")
        # الحالة مربوطة بالمستخدم والجلسة، قصيرة العمر، وتُستخدم مرة واحدة.
        if (not expected or not state or not secrets.compare_digest(expected, state)
                or pending.get("uid") != request.env.uid
                or pending.get("expires", 0) < time.time()):
            raise BadRequest("Expired or invalid Google authorization state.")
        if error or not code:
            raise BadRequest("Google authorization was not completed.")
        storage = request.env["fs.storage"].browse(pending.get("storage_id")).exists()
        if not storage or storage.protocol != "fastegy_gdrive" or storage.fd_clone_readonly:
            raise BadRequest("Drive storage is unavailable in this database.")
        try:
            response = requests.post("https://oauth2.googleapis.com/token", data={
                "code": code, "client_id": storage.fd_client_id,
                "client_secret": storage._fd_secret("fd_client_secret"),
                "redirect_uri": pending["redirect_uri"], "grant_type": "authorization_code",
            }, timeout=20)
            response.raise_for_status()
            payload = response.json()
            token = payload.get("refresh_token")
        except (requests.RequestException, ValueError) as exc:
            raise BadRequest("Google token exchange failed. Authorize again.") from exc
        if not token:
            raise BadRequest("Google did not return a refresh token. Revoke the app grant and authorize again.")
        values = {"fd_refresh_token": token, "fd_allow_writes": False, "fd_readonly": True}
        if not storage.fd_root_folder_id:
            # drive.file يتيح مجلدًا أنشأه تطبيقنا دون صلاحية لكل ملفات الحساب.
            # عند ضياع الرد لا نعيد إنشاء المجلد تلقائيًا في نفس الطلب.
            try:
                created = requests.post(
                    "https://www.googleapis.com/drive/v3/files",
                    headers={"Authorization": "Bearer " + payload["access_token"]},
                    params={"fields": "id"}, json={
                        "name": "FastEgy Odoo — " + request.env.cr.dbname,
                        "mimeType": "application/vnd.google-apps.folder",
                        "appProperties": {"fastegy_namespace": storage.fd_namespace},
                    }, timeout=20,
                )
                created.raise_for_status()
                values["fd_root_folder_id"] = created.json()["id"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                raise BadRequest("Google folder creation failed. Check Drive before authorizing again.") from exc
        storage.write(values)
        action_id = request.env.ref("fastegy_drive_storage.action_drive_storage").id
        return request.redirect("/web#id=%s&model=fs.storage&view_type=form&action=%s" % (storage.id, action_id))


