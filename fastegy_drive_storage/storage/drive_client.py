"""Small Drive API v3 client. File bodies and OAuth tokens stay in memory."""

import hashlib
import json
import re
import time
import uuid
from urllib.parse import urlsplit

import requests

API_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"
NAMESPACE_PROPERTY = "fastegy_namespace"
PATH_PROPERTY = "fastegy_path_sha256"
FILE_FIELDS = "id,name,size,md5Checksum,mimeType,parents,appProperties,description,trashed,driveId,createdTime,modifiedTime,capabilities,shortcutDetails"


class DriveBackendError(OSError):
    """A remote operation failed; messages never contain response bodies or tokens."""


class DuplicateRemotePathError(FileExistsError):
    """Several remote files claim the same logical path."""


class RemoteIntegrityError(DriveBackendError):
    """Remote metadata or bytes did not match the requested file."""


class DriveAPIError(DriveBackendError):
    def __init__(self, category, transient=False):
        self.category, self.transient = category, transient
        super().__init__("Google Drive operation failed: " + category)


def error_reason(response):
    # لا نمرر نص جوجل إلى السجلات لأنه قد يحتوي بيانات خاصة.
    try:
        errors = response.json().get("error", {}).get("errors", [])
        return errors[0].get("reason", "") if errors else ""
    except (ValueError, TypeError, AttributeError, IndexError):
        return ""


def content_md5(data):
    # جوجل يستخدم هذه البصمة لمقارنة سلامة النقل، لا لحماية كلمات المرور.
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class DriveClient:
    """Only manages binary files in one explicit folder and one UUID namespace."""

    def __init__(
        self, client_id, client_secret, refresh_token, root_folder_id, namespace,
        max_file_bytes=20971520, timeout=20, session=None, readonly=False,
        shared_drive_id=None, chunk_bytes=1048576, resumable_threshold=5242880,
    ):
        if not all((client_id, client_secret, refresh_token)):
            raise ValueError("Google OAuth client ID, secret and refresh token are required")
        if root_folder_id == "root" or not re.fullmatch(r"[A-Za-z0-9_-]+", root_folder_id or ""):
            raise ValueError("An explicit Google Drive folder ID is required")
        self.namespace = str(uuid.UUID(str(namespace)))
        self.root_folder_id = root_folder_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.max_file_bytes = int(max_file_bytes)
        self.timeout = float(timeout)
        if self.max_file_bytes <= 0 or not 0 < self.timeout <= 120:
            raise ValueError("Invalid file limit or HTTP timeout")
        self.session = session if session is not None else requests.Session()
        self.readonly = bool(readonly)
        self._token = None
        self._token_deadline = 0
        self.shared_drive_id = shared_drive_id
        if shared_drive_id and not re.fullmatch(r"[A-Za-z0-9_-]+", shared_drive_id):
            raise ValueError("Invalid shared drive ID")
        self.chunk_bytes = int(chunk_bytes)
        self.resumable_threshold = int(resumable_threshold)
        if self.chunk_bytes < 262144 or self.chunk_bytes % 262144:
            raise ValueError("Upload chunks must be multiples of 256 KiB")
        self._known_folders = {root_folder_id}

    def _refresh(self):
        try:
            response = self.session.request(
                "POST", TOKEN_URL,
                data={"client_id": self.client_id, "client_secret": self.client_secret,
                      "refresh_token": self.refresh_token, "grant_type": "refresh_token"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise DriveBackendError("Google OAuth refresh failed") from exc
        try:
            if response.status_code != 200:
                raise PermissionError("Google OAuth refresh was rejected; reconnect the account")
            try:
                result = response.json()
                token = result["access_token"]
                expires = int(result.get("expires_in", 3600))
            except (KeyError, TypeError, ValueError) as exc:
                raise DriveBackendError("Invalid Google OAuth response") from exc
            if not isinstance(token, str) or not token or expires <= 0:
                raise DriveBackendError("Invalid Google OAuth token")
            self._token = token
            self._token_deadline = time.monotonic() + max(1, expires - 60)
        finally:
            response.close()

    def _request(self, method, url, *, retry_safe=False, **kwargs):
        """Retry GETs and operations whose identity is already fixed; never log bodies."""
        refreshed = False
        attempts = 3 if retry_safe else 1
        base_headers = dict(kwargs.pop("headers", {}))
        for attempt in range(attempts):
            if not self._token or time.monotonic() >= self._token_deadline:
                self._refresh()
            headers = dict(base_headers)
            headers["Authorization"] = "Bearer " + self._token
            try:
                response = self.session.request(
                    method, url, headers=headers, timeout=self.timeout, **kwargs
                )
                if response.status_code == 401 and not refreshed:
                    response.close()
                    self._refresh()
                    refreshed = True
                    headers["Authorization"] = "Bearer " + self._token
                    response = self.session.request(
                        method, url, headers=headers, timeout=self.timeout, **kwargs
                    )
            except requests.RequestException as exc:
                if attempt + 1 == attempts:
                    raise DriveBackendError("Google Drive request failed or timed out") from exc
                time.sleep(0.25 * (2 ** attempt))
                continue
            retryable = response.status_code in (429, 500, 502, 503, 504) or (
                response.status_code == 403 and error_reason(response) in
                {"rateLimitExceeded", "userRateLimitExceeded", "backendError"})
            if retryable and attempt + 1 < attempts:
                try:
                    delay = min(30, max(0.25 * (2 ** attempt), float(getattr(response, "headers", {}).get("Retry-After", 0))))
                except (ValueError, TypeError):
                    delay = 0.25 * (2 ** attempt)
                response.close()
                time.sleep(delay)
                continue
            return response
        raise DriveBackendError("Google Drive request failed")

    @staticmethod
    def _check_status(response, allowed=(200,)):
        status = response.status_code
        if status in allowed:
            return
        if status == 404:
            raise FileNotFoundError("Google Drive file or folder is unavailable")
        reason = error_reason(response)
        if reason in {"storageQuotaExceeded", "teamDriveFileLimitExceeded", "numChildrenInNonRootLimitExceeded"}:
            raise DriveAPIError("quota_exceeded")
        if status == 429 or reason in {"rateLimitExceeded", "userRateLimitExceeded"}:
            raise DriveAPIError("rate_limited", True)
        if status >= 500:
            raise DriveAPIError("service_unavailable", True)
        if status in (401, 403):
            raise PermissionError("Google Drive denied access")
        raise DriveBackendError("Google Drive request failed (HTTP %s)" % status)

    def _json(self, method, url, *, allowed=(200,), retry_safe=False, **kwargs):
        response = self._request(method, url, retry_safe=retry_safe, **kwargs)
        try:
            self._check_status(response, allowed)
            try:
                result = response.json()
            except (ValueError, TypeError) as exc:
                raise DriveBackendError("Invalid Google Drive response") from exc
            if not isinstance(result, dict):
                raise DriveBackendError("Invalid Google Drive response")
            return result
        finally:
            response.close()

    def validate_root(self):
        result = self._json(
            "GET", API_URL + "/" + self.root_folder_id, retry_safe=True,
            params={"fields": "id,name,mimeType,trashed,driveId,capabilities", "supportsAllDrives": "true"},
        )
        if (result.get("id") != self.root_folder_id
                or result.get("mimeType") != "application/vnd.google-apps.folder"
                or result.get("trashed")):
            raise RemoteIntegrityError("Configured Google Drive root is not an active folder")
        if self.shared_drive_id and result.get("driveId") != self.shared_drive_id:
            raise RemoteIntegrityError("Folder does not belong to the configured shared drive")
        return result

    check_connection = validate_root

    @staticmethod
    def path_digest(path):
        return hashlib.sha256(path.encode("utf-8")).hexdigest()

    def properties(self, path):
        return {NAMESPACE_PROPERTY: self.namespace, PATH_PROPERTY: self.path_digest(path)}

    def describe(self, path):
        return json.dumps({"schema": 1, "path": path}, ensure_ascii=False, separators=(",", ":"))

    def file_path(self, metadata):
        try:
            path = json.loads(metadata["description"])["path"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteIntegrityError("Managed file has invalid path metadata") from exc
        if not isinstance(path, str) or not path or metadata.get("appProperties") != self.properties(path):
            raise RemoteIntegrityError("Managed file path metadata does not match")
        if not self._inside_root(metadata.get("parents", [])) or metadata.get("trashed"):
            raise RemoteIntegrityError("Managed file is outside its configured folder")
        return path

    def _inside_root(self, parents):
        # نفحص سلسلة الآباء الفعلية؛ ادعاء الجذر في خصائص الملف وحده غير كاف.
        seen = set()
        for _ in range(100):
            if self.root_folder_id in parents:
                return True
            if len(parents) != 1 or parents[0] in seen:
                return False
            parent = parents[0]
            seen.add(parent)
            folder = self.metadata(parent)
            if folder.get("trashed") or folder.get("mimeType") != "application/vnd.google-apps.folder":
                return False
            parents = folder.get("parents", [])
        return False

    def list_file_page(self, path=None, token=None, page_size=100):
        if not 1 <= page_size <= 1000:
            raise ValueError("Invalid Drive page size")
        query = "trashed = false and appProperties has { key='%s' and value='%s' }" % (NAMESPACE_PROPERTY, self.namespace)
        # يحافظ المسار القديم على بحث الجذر، أما المسار المتداخل فيبحث بالهوية.
        if not getattr(self, "nested_layout", False):
            query = "'%s' in parents and " % self.root_folder_id + query
        if path is not None:
            query += " and appProperties has { key='%s' and value='%s' }" % (PATH_PROPERTY, self.path_digest(path))
        params = {"q": query, "fields": "nextPageToken,incompleteSearch,files(%s)" % FILE_FIELDS,
                  "pageSize": page_size, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if token:
            params["pageToken"] = token
        if self.shared_drive_id:
            params.update(corpora="drive", driveId=self.shared_drive_id)
        result = self._json("GET", API_URL, params=params, retry_safe=True)
        page = result.get("files")
        if not isinstance(page, list) or len(page) > page_size:
            raise DriveBackendError("Invalid Google Drive file list")
        if result.get("incompleteSearch"):
            raise DriveBackendError("Google Drive returned an incomplete search")
        files = []
        for metadata in page:
            metadata = self._expand_reference(metadata)
            actual_path = self.file_path(metadata)
            if path is not None and actual_path != path:
                raise RemoteIntegrityError("Google Drive returned an unexpected logical path")
            files.append(metadata)
        next_token = result.get("nextPageToken")
        if next_token and (not isinstance(next_token, str) or next_token == token):
            raise DriveBackendError("Google Drive repeated an invalid pagination token")
        return files, next_token

    def list_files(self, path=None):
        token = None
        seen_tokens = set()
        files = []
        while True:
            page, token = self.list_file_page(path, token, 1000)
            files.extend(page)
            if not token:
                return files
            if token in seen_tokens:
                raise DriveBackendError("Google Drive repeated a pagination token")
            seen_tokens.add(token)

    def lookup(self, path):
        matches = self.list_files(path)
        if len(matches) > 1:
            raise DuplicateRemotePathError("Multiple Google Drive files claim the same path")
        return matches[0] if matches else None

    def metadata(self, file_id):
        result = self._json("GET", API_URL + "/" + file_id, retry_safe=True,
                            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"})
        return self._expand_reference(result)

    def _expand_reference(self, metadata):
        if metadata.get("mimeType") != "application/vnd.google-apps.shortcut":
            return metadata
        try:
            reference = json.loads(metadata["description"])["reference"]
            if reference["target_id"] != metadata["shortcutDetails"]["targetId"]:
                raise ValueError()
            result = dict(metadata, size=str(reference["size"]), md5Checksum=reference["md5"])
            self.file_path(dict(result, trashed=False))
            return result
        except (KeyError, ValueError, TypeError):
            raise RemoteIntegrityError("Unverified Drive shortcut reference") from None

    def _media_id(self, metadata):
        if metadata.get("shortcutDetails"):
            reference = json.loads(metadata["description"])["reference"]
            target = self.metadata(reference["target_id"])
            if (target.get("trashed") or target.get("shortcutDetails")
                    or str(target.get("size")) != str(reference["size"])
                    or target.get("md5Checksum") != reference["md5"]):
                raise RemoteIntegrityError("The referenced Drive file changed; review and reattach it")
            return reference["target_id"]
        return metadata["id"]

    def verify(self, metadata, path, data):
        if self.file_path(metadata) != path:
            raise RemoteIntegrityError("Google Drive file identity does not match")
        if str(len(data)) != str(metadata.get("size")) or metadata.get("md5Checksum") != content_md5(data):
            raise RemoteIntegrityError("Google Drive file checksum or size does not match")

    def download(self, metadata):
        self.file_path(metadata)
        try:
            expected_size = int(metadata["size"])
        except (KeyError, ValueError, TypeError) as exc:
            raise RemoteIntegrityError("Managed file has invalid size metadata") from exc
        if not 0 <= expected_size <= self.max_file_bytes:
            raise ValueError("File exceeds the configured in-memory size limit")
        response = self._request("GET", API_URL + "/" + self._media_id(metadata), retry_safe=True,
                                 params={"alt": "media", "supportsAllDrives": "true"}, stream=True)
        try:
            self._check_status(response)
            data = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=65536):
                    if len(data) + len(chunk) > self.max_file_bytes:
                        raise ValueError("Downloaded file exceeds the in-memory size limit")
                    data.extend(chunk)
            except requests.RequestException as exc:
                raise DriveBackendError("Google Drive download was interrupted") from exc
            content = bytes(data)
            self.verify(metadata, self.file_path(metadata), content)
            return content
        finally:
            response.close()

    def upload(self, path, data, mimetype):
        if self.readonly:
            raise PermissionError("Google Drive storage is read-only")
        if len(data) > self.max_file_bytes:
            raise ValueError("File exceeds the configured in-memory size limit")
        existing = self.lookup(path)
        if existing:
            self.verify(existing, path, data)
            return existing
        generated = self._json("GET", API_URL + "/generateIds", retry_safe=True,
                               params={"count": 1, "space": "drive", "type": "files"})
        try:
            file_id = generated["ids"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise DriveBackendError("Google Drive did not allocate a file ID") from exc
        if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
            raise DriveBackendError("Invalid Google Drive allocated file ID")
        metadata = {"id": file_id, "name": path.rsplit("/", 1)[-1],
                    "parents": [self.root_folder_id], "appProperties": self.properties(path),
                    "description": self.describe(path), "mimeType": mimetype}
        if len(data) > self.resumable_threshold:
            self._upload_resumable(metadata, data, mimetype)
            uploaded = self.metadata(file_id)
            self.verify(uploaded, path, data)
            return uploaded
        boundary = "fastegy_" + uuid.uuid4().hex
        body = ("--%s\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n%s\r\n--%s\r\nContent-Type: %s\r\n\r\n" % (
            boundary, json.dumps(metadata), boundary, mimetype)).encode("utf-8")
        body += data + ("\r\n--%s--\r\n" % boundary).encode("ascii")
        response = self._request(
            "POST", UPLOAD_URL, retry_safe=True,
            params={"uploadType": "multipart", "fields": FILE_FIELDS, "supportsAllDrives": "true"},
            headers={"Content-Type": "multipart/related; boundary=" + boundary}, data=body,
        )
        try:
            # تكرار نفس المعرف بعد مهلة غامضة لا ينشئ ملفاً ثانياً؛ نتحقق من النسخة الموجودة.
            self._check_status(response, (200, 201, 409))
        finally:
            response.close()
        uploaded = self.metadata(file_id)
        self.verify(uploaded, path, data)
        found = self.lookup(path)
        if not found or found["id"] != file_id:
            raise RemoteIntegrityError("Uploaded Google Drive file cannot be located")
        return uploaded

    def _upload_resumable(self, metadata, data, mimetype):
        response = self._request("POST", UPLOAD_URL, retry_safe=False,
            params={"uploadType": "resumable", "fields": FILE_FIELDS, "supportsAllDrives": "true"},
            headers={"X-Upload-Content-Type": mimetype, "X-Upload-Content-Length": str(len(data))}, json=metadata)
        try:
            self._check_status(response, (200, 201, 409))
            if response.status_code == 409:
                return
            session_url = response.headers.get("Location", "")
            parsed = urlsplit(session_url)
            if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com" or not parsed.path.startswith("/upload/drive/"):
                raise RemoteIntegrityError("Invalid Google upload session destination")
        finally:
            response.close()
        offset, stalled = 0, 0
        while offset < len(data):
            end = min(offset + self.chunk_bytes, len(data))
            response = None
            try:
                response = self._request("PUT", session_url, retry_safe=False,
                    headers={"Content-Type": mimetype, "Content-Length": str(end-offset),
                             "Content-Range": "bytes %s-%s/%s" % (offset, end-1, len(data))}, data=data[offset:end])
                if response.status_code >= 500 or response.status_code in (429, 403):
                    self._check_status(response)
            except (DriveBackendError, requests.RequestException) as exc:
                if response is not None:
                    response.close()
                if isinstance(exc, DriveAPIError) and not exc.transient:
                    raise
                stalled += 1
                if stalled > 3:
                    raise DriveAPIError("upload_interrupted", True) from exc
                response = self._request("PUT", session_url, retry_safe=True,
                    headers={"Content-Length": "0", "Content-Range": "bytes */%s" % len(data)}, data=b"")
            except PermissionError:
                if response is not None:
                    response.close()
                raise
            try:
                self._check_status(response, (200, 201, 308))
                if response.status_code in (200, 201):
                    return
                accepted = getattr(response, "headers", {}).get("Range", "")
                match = re.fullmatch(r"bytes=0-(\d+)", accepted)
                next_offset = int(match.group(1)) + 1 if match else 0
                if next_offset < offset or next_offset > end:
                    raise RemoteIntegrityError("Invalid upload acknowledgement")
                stalled = stalled + 1 if next_offset == offset else 0
                if stalled > 3:
                    raise DriveAPIError("upload_stalled", True)
                offset = next_offset
            finally:
                response.close()

    def about(self):
        return self._json("GET", "https://www.googleapis.com/drive/v3/about", retry_safe=True,
                          params={"fields": "storageQuota,user(displayName,emailAddress)"})

    def ensure_folder(self, name, parent_id=None):
        if self.readonly:
            raise PermissionError("Google Drive storage is read-only")
        parent_id = parent_id or self.root_folder_id
        if parent_id != self.root_folder_id and not self._inside_root([parent_id]):
            raise PermissionError("Folder parent is outside managed root")
        key = hashlib.sha256((parent_id + "/" + name).encode()).hexdigest()
        q = "'%s' in parents and trashed=false and mimeType='application/vnd.google-apps.folder' and appProperties has { key='fastegy_folder' and value='%s' }" % (parent_id, key)
        params = {"q": q, "fields": "files(id,name,parents)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if self.shared_drive_id:
            params.update(corpora="drive", driveId=self.shared_drive_id)
        found = self._json("GET", API_URL, retry_safe=True, params=params).get("files", [])
        if len(found) > 1:
            raise DuplicateRemotePathError("Duplicate managed folder identity")
        if found:
            return found[0]["id"]
        return self._json("POST", API_URL, json={"name": name[:180], "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id], "appProperties": {"fastegy_folder": key}},
            params={"fields": "id", "supportsAllDrives": "true"})["id"]

    def update_presentation(self, metadata, name, mimetype, folders=()):
        self.file_path(metadata)
        if self.readonly:
            raise PermissionError("Google Drive storage is read-only")
        parent = self.root_folder_id
        for folder in folders:
            parent = self.ensure_folder(folder, parent)
        params = {"fields": FILE_FIELDS, "supportsAllDrives": "true"}
        if metadata.get("parents") != [parent]:
            params.update(addParents=parent, removeParents=",".join(metadata.get("parents", [])))
        values = {"name": name[:240]}
        if not metadata.get("shortcutDetails"):
            values["mimeType"] = mimetype
        return self._expand_reference(self._json("PATCH", API_URL + "/" + metadata["id"], retry_safe=True, params=params, json=values))

    def create_reference(self, path, target):
        if self.readonly or target.get("trashed") or target.get("mimeType", "").startswith("application/vnd.google-apps."):
            raise PermissionError("Only authorized binary files can be referenced")
        details = {"schema": 1, "path": path, "reference": {"target_id": target["id"],
                   "size": int(target["size"]), "md5": target["md5Checksum"]}}
        result = self._json("POST", API_URL, params={"fields": FILE_FIELDS, "supportsAllDrives": "true"}, json={
            "name": target["name"], "mimeType": "application/vnd.google-apps.shortcut", "parents": [self.root_folder_id],
            "shortcutDetails": {"targetId": target["id"]}, "appProperties": self.properties(path),
            "description": json.dumps(details, ensure_ascii=False)})
        return self._expand_reference(result)

    def restore(self, file_id):
        if self.readonly:
            raise PermissionError("Google Drive storage is read-only")
        metadata = self.metadata(file_id)
        # استعادة بيانات وصفية موثقة فقط دون السماح بتبني ملف خارجي عشوائي.
        candidate = dict(metadata, trashed=False)
        self.file_path(candidate)
        return self._json("PATCH", API_URL + "/" + file_id, retry_safe=True,
                          params={"fields": FILE_FIELDS, "supportsAllDrives": "true"}, json={"trashed": False})

    def iter_download(self, metadata, chunk_size=262144):
        self.file_path(metadata)
        expected = int(metadata.get("size", -1))
        if not 0 <= expected <= self.max_file_bytes:
            raise ValueError("File exceeds configured download limit")
        response = self._request("GET", API_URL + "/" + self._media_id(metadata), retry_safe=True,
            params={"alt": "media", "supportsAllDrives": "true"}, stream=True)
        digest, received = hashlib.md5(usedforsecurity=False), 0
        try:
            self._check_status(response)
            for chunk in response.iter_content(chunk_size=chunk_size):
                received += len(chunk)
                if received > expected:
                    raise RemoteIntegrityError("Download exceeds declared size")
                digest.update(chunk)
                yield chunk
            if received != expected or digest.hexdigest() != metadata.get("md5Checksum"):
                raise RemoteIntegrityError("Downloaded checksum or size mismatch")
        finally:
            response.close()

    def iter_range(self, metadata, start, end):
        self.file_path(metadata)
        size = int(metadata["size"])
        if not 0 <= start <= end < size <= self.max_file_bytes:
            raise ValueError("Invalid bounded download range")
        response = self._request("GET", API_URL + "/" + self._media_id(metadata), retry_safe=True,
            params={"alt": "media", "supportsAllDrives": "true"}, headers={"Range": "bytes=%s-%s" % (start, end)}, stream=True)
        count = 0
        try:
            self._check_status(response, (206,))
            if response.headers.get("Content-Range") != "bytes %s-%s/%s" % (start, end, size):
                raise RemoteIntegrityError("Drive returned an unexpected content range")
            for chunk in response.iter_content(262144):
                count += len(chunk)
                if count > end-start+1:
                    raise RemoteIntegrityError("Range download exceeded requested size")
                yield chunk
            if count != end-start+1:
                raise RemoteIntegrityError("Range download was incomplete")
        finally:
            response.close()

    def move(self, metadata, target_path):
        if self.readonly:
            raise PermissionError("Google Drive storage is read-only")
        self.file_path(metadata)
        return self._json(
            "PATCH", API_URL + "/" + metadata["id"], retry_safe=True,
            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
            json={"name": target_path.rsplit("/", 1)[-1], "description": self.describe(target_path),
                  "appProperties": self.properties(target_path)},
        )

    def trash(self, metadata):
        if self.readonly:
            raise PermissionError("Google Drive storage is read-only")
        self.file_path(metadata)
        result = self._json("PATCH", API_URL + "/" + metadata["id"], retry_safe=True,
                            params={"fields": "id,trashed", "supportsAllDrives": "true"},
                            json={"trashed": True})
        if not result.get("trashed"):
            raise RemoteIntegrityError("Google Drive did not confirm moving the file to trash")


GoogleDriveClient = DriveClient
