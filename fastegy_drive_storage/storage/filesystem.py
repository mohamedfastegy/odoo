"""Memory-only, bounded fsspec adapter for an isolated Google Drive folder."""

import io
import hashlib
import mimetypes
import posixpath
import threading

from fsspec.spec import AbstractFileSystem

from .drive_client import DriveClient, DuplicateRemotePathError, RemoteIntegrityError


class _UploadBuffer(io.BytesIO):
    def __init__(self, fs, path, mimetype=None):
        super().__init__()
        self.fs = fs
        self.path = path
        self.aborted = False
        self.mimetype = mimetype

    def write(self, data):
        if max(len(self.getbuffer()), self.tell() + len(data)) > self.fs.max_file_bytes:
            self.aborted = True
            raise ValueError("File exceeds the configured in-memory size limit")
        return super().write(data)

    def truncate(self, size=None):
        size = self.tell() if size is None else size
        if size > self.fs.max_file_bytes:
            self.aborted = True
            raise ValueError("File exceeds the configured in-memory size limit")
        return super().truncate(size)

    def close(self):
        if self.closed:
            return
        try:
            if not self.aborted:
                self.fs._store(self.path, self.getvalue(), self.mimetype)
        finally:
            super().close()

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            self.aborted = True
        self.close()
        return False


class FastEgyGoogleDriveFileSystem(AbstractFileSystem):
    """Private immutable blobs; directories are virtual, never local byte caches."""

    protocol = "fastegy_gdrive"
    cachable = False
    root_marker = ""

    def __init__(
        self, client_id, client_secret, refresh_token, root_folder_id, namespace,
        readonly=False, max_file_bytes=20971520, timeout=20, session=None,
        shared_drive_id=None, nested_layout=False, **kwargs,
    ):
        super().__init__(**kwargs)
        self.readonly = bool(readonly)
        self.max_file_bytes = int(max_file_bytes)
        self.client = DriveClient(client_id, client_secret, refresh_token, root_folder_id,
                                  namespace, self.max_file_bytes, timeout, session,
                                  readonly=readonly, shared_drive_id=shared_drive_id)
        self.client.nested_layout = nested_layout
        self._lock = threading.RLock()

    def _path(self, path):
        value = str(self._strip_protocol(path))
        if "\x00" in value or "\\" in value or any(p == ".." for p in value.split("/")):
            raise ValueError("Invalid logical Google Drive path")
        return posixpath.normpath(value).strip("/") if value not in ("", "/", ".") else ""

    def _writable(self):
        if self.readonly:
            raise PermissionError("Google Drive storage is read-only")

    def validate_root(self):
        return self.client.validate_root()

    def _metadata(self, path):
        metadata = self.client.lookup(path)
        if metadata is None:
            raise FileNotFoundError(path)
        return metadata

    def _entry(self, metadata):
        path = self.client.file_path(metadata)
        if self._path(path) != path:
            raise RemoteIntegrityError("Managed file has a noncanonical logical path")
        return {"name": path, "size": int(metadata["size"]), "type": "file",
                "id": metadata["id"], "md5Checksum": metadata["md5Checksum"]}

    def info(self, path, **kwargs):
        path = self._path(path)
        if not path:
            self.validate_root()
            return {"name": "", "size": 0, "type": "directory"}
        metadata = self.client.lookup(path)
        if metadata is not None:
            return self._entry(metadata)
        prefix = path + "/"
        if any(self.client.file_path(m).startswith(prefix) for m in self.client.list_files()):
            return {"name": path, "size": 0, "type": "directory"}
        raise FileNotFoundError(path)

    def exists(self, path, **kwargs):
        try:
            self.info(path)
            return True
        except FileNotFoundError:
            return False

    def ls(self, path, detail=True, **kwargs):
        path = self._path(path)
        exact = self.client.lookup(path) if path else None
        if exact:
            entries = [self._entry(exact)]
        else:
            prefix = path + "/" if path else ""
            entries_by_name = {}
            seen_paths = set()
            for metadata in self.client.list_files():
                file_path = self.client.file_path(metadata)
                if file_path in seen_paths:
                    raise DuplicateRemotePathError("Multiple Google Drive files claim the same path")
                seen_paths.add(file_path)
                if not file_path.startswith(prefix):
                    continue
                rest = file_path[len(prefix):]
                if "/" in rest:
                    name = prefix + rest.split("/", 1)[0]
                    entries_by_name.setdefault(name, {"name": name, "size": 0, "type": "directory"})
                else:
                    entries_by_name[file_path] = self._entry(metadata)
            if path and not entries_by_name:
                raise FileNotFoundError(path)
            if not path:
                self.validate_root()
            entries = [entries_by_name[key] for key in sorted(entries_by_name)]
        return entries if detail else [entry["name"] for entry in entries]

    def makedirs(self, path, exist_ok=False):
        # المسارات منطقية داخل مجلد فعلي واحد، فلا نكتب مجلدات أو ملفات محلية.
        self._writable()
        self._path(path)

    def mkdir(self, path, create_parents=True, **kwargs):
        self.makedirs(path, exist_ok=True)

    def _open(self, path, mode="rb", **kwargs):
        path = self._path(path)
        if not path:
            raise IsADirectoryError(path)
        if mode == "rb":
            return io.BytesIO(self.client.download(self._metadata(path)))
        if mode == "wb":
            self._writable()
            return _UploadBuffer(self, path, kwargs.get("mimetype"))
        raise NotImplementedError("Only binary rb and wb modes are supported")

    def _store(self, path, data, mimetype=None):
        self._writable()
        path = self._path(path)
        if not path:
            raise IsADirectoryError("The Google Drive root is not a file")
        with self._lock:
            metadata = self.client.lookup(path)
            if metadata:
                try:
                    self.client.verify(metadata, path, data)
                except RemoteIntegrityError as exc:
                    raise FileExistsError("Immutable Google Drive path already has different content") from exc
                return
            self.client.upload(path, data, mimetype or mimetypes.guess_type(path)[0] or "application/octet-stream")

    def cp_file(self, path1, path2, **kwargs):
        self._writable()
        source, target = self._path(path1), self._path(path2)
        with self.open(source, "rb") as stream:
            self._store(target, stream.read())

    def copy(self, path1, path2, recursive=False, **kwargs):
        if recursive:
            raise NotImplementedError("Recursive copy is not supported")
        return self.cp_file(path1, path2, **kwargs)

    def mv(self, path1, path2, recursive=False, **kwargs):
        self._writable()
        if recursive:
            raise NotImplementedError("Recursive move is not supported")
        source, target = self._path(path1), self._path(path2)
        if not target:
            raise IsADirectoryError("The Google Drive root is not a file")
        with self._lock:
            metadata = self._metadata(source)
            if source == target:
                return
            if self.client.lookup(target):
                raise FileExistsError("Destination Google Drive path already exists")
            moved = self.client.move(metadata, target)
            if self.client.file_path(moved) != target or moved.get("id") != metadata["id"]:
                raise RemoteIntegrityError("Google Drive move identity was not confirmed")

    def rm_file(self, path):
        self._writable()
        path = self._path(path)
        if not path:
            raise PermissionError("The Google Drive root cannot be removed")
        self.client.trash(self._metadata(path))

    def checksum(self, path):
        # أودو يضع هذه القيمة في بصمة المرفق؛ بصمة جوجل للنقل تستخدم خوارزمية مختلفة.
        content = self.client.download(self._metadata(self._path(path)))
        return hashlib.sha1(content, usedforsecurity=False).hexdigest()
