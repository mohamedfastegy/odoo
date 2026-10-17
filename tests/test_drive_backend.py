"""Offline contract tests. FakeSession is the only HTTP transport used here."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import uuid

import pytest
import requests

STORAGE = Path(__file__).resolve().parents[1] / "fastegy_drive_storage" / "storage"
SPEC = importlib.util.spec_from_file_location(
    "_fastegy_backend_test", STORAGE / "__init__.py", submodule_search_locations=[str(STORAGE)]
)
BACKEND = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BACKEND
SPEC.loader.exec_module(BACKEND)
DriveBackendError = BACKEND.DriveBackendError
DuplicateRemotePathError = BACKEND.DuplicateRemotePathError
RemoteIntegrityError = BACKEND.RemoteIntegrityError
FS = BACKEND.FastEgyGoogleDriveFileSystem
CLIENT_MODULE = sys.modules[SPEC.name + ".drive_client"]
API = CLIENT_MODULE.API_URL
UPLOAD = CLIENT_MODULE.UPLOAD_URL
TOKEN = CLIENT_MODULE.TOKEN_URL
NAMESPACE = "42b9f091-32da-4f92-81b8-f0b028388c15"


class FakeResponse:
    def __init__(self, status=200, payload=None, body=b"", broken_stream=False):
        self.status_code = status
        self.payload = payload if payload is not None else {}
        self.body = body
        self.closed = False
        self.broken_stream = broken_stream

    def json(self):
        return copy.deepcopy(self.payload)

    def iter_content(self, chunk_size):
        yield self.body[:chunk_size]
        if self.broken_stream:
            raise requests.ConnectionError("network interrupted")
        if len(self.body) > chunk_size:
            yield self.body[chunk_size:]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.files = {}
        self.calls = []
        self.responses = []
        self.failures = {}
        self.next_id = 0
        self.page_size = 1000
        self.timeout_after_create = False
        self.break_download = False

    @staticmethod
    def metadata(file):
        return {key: value for key, value in copy.deepcopy(file).items() if key != "_data"}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        assert 0 < kwargs["timeout"] <= 120
        failure = self.failures.get((method, url), [])
        if failure:
            result = failure.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if url == TOKEN:
            assert kwargs["data"]["grant_type"] == "refresh_token"
            result = FakeResponse(payload={"access_token": "test-access-token", "expires_in": 3600})
        elif url == API + "/generateIds":
            self.next_id += 1
            result = FakeResponse(payload={"ids": ["file_%s" % self.next_id]})
        elif url == API + "/private_folder":
            result = FakeResponse(payload={"id": "private_folder", "name": "FastEgy test",
                                           "mimeType": "application/vnd.google-apps.folder", "trashed": False})
        elif method == "GET" and url == API:
            params = kwargs["params"]
            query = params["q"]
            parent = re.search(r"'([^']+)' in parents", query).group(1)
            properties = re.findall(r"key='([^']+)' and value='([^']+)'", query)
            files = [self.metadata(file) for file in self.files.values()
                     if parent in file["parents"] and not file.get("trashed")
                     and all(file["appProperties"].get(k) == v for k, v in properties)]
            start = int(params.get("pageToken", 0))
            page = files[start:start + self.page_size]
            payload = {"files": page}
            if start + self.page_size < len(files):
                payload["nextPageToken"] = str(start + self.page_size)
            result = FakeResponse(payload=payload)
        elif method == "POST" and url == UPLOAD:
            header = kwargs["headers"]["Content-Type"]
            boundary = header.split("boundary=")[1].encode()
            pieces = kwargs["data"].split(b"--" + boundary)
            metadata = json.loads(pieces[1].split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n"))
            data = pieces[2].split(b"\r\n\r\n", 1)[1][:-2]
            if metadata["id"] in self.files:
                result = FakeResponse(409)
            else:
                metadata.update(size=str(len(data)), md5Checksum=hashlib.md5(data).hexdigest(),
                                trashed=False, _data=data)
                self.files[metadata["id"]] = metadata
                if self.timeout_after_create:
                    self.timeout_after_create = False
                    raise requests.Timeout("response lost after commit")
                result = FakeResponse(payload=self.metadata(metadata))
        elif url.startswith(API + "/"):
            file_id = url.rsplit("/", 1)[1]
            file = self.files.get(file_id)
            if not file:
                result = FakeResponse(404)
            elif method == "GET" and kwargs.get("params", {}).get("alt") == "media":
                result = FakeResponse(body=file["_data"], broken_stream=self.break_download)
            elif method == "GET":
                result = FakeResponse(payload=self.metadata(file))
            elif method == "PATCH":
                file.update(kwargs["json"])
                result = FakeResponse(payload=self.metadata(file))
            else:
                raise AssertionError("Unexpected HTTP operation: %s %s" % (method, url))
        else:
            raise AssertionError("Unexpected HTTP request: %s %s" % (method, url))
        self.responses.append(result)
        return result


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(CLIENT_MODULE.time, "sleep", lambda seconds: None)


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def fs(session):
    return FS("client-id", "client-secret", "refresh-token", "private_folder", NAMESPACE,
              session=session)


def write(fs, path="photos/proof.jpg", data=b"photo content"):
    with fs.open(path, "wb") as stream:
        stream.write(data)


def test_roundtrip_filelike_seek_info_and_virtual_directories(fs, session):
    write(fs, "photos/اثبات.jpg", b"0123456789")
    with fs.open("photos/اثبات.jpg", "rb") as stream:
        stream.seek(4)
        assert stream.read(3) == b"456"
        stream.seek(-2, 2)
        assert stream.read() == b"89"
    assert fs.exists("photos")
    assert fs.info("photos")["type"] == "directory"
    assert fs.ls("", detail=False) == ["photos"]
    assert fs.ls("photos")[0]["size"] == 10
    assert fs.checksum("photos/اثبات.jpg") == hashlib.sha1(b"0123456789").hexdigest()
    assert fs.info("photos/اثبات.jpg")["md5Checksum"] == hashlib.md5(b"0123456789").hexdigest()
    assert len(session.files) == 1
    assert all(response.closed for response in session.responses)


def test_root_connection_is_real_and_alias_matches(fs):
    assert fs.validate_root()["id"] == "private_folder"
    assert fs.client.check_connection()["mimeType"] == "application/vnd.google-apps.folder"
    assert fs.info("/")["type"] == "directory"


@pytest.mark.parametrize("root", ["", "root", "../../bad", "folder?query"])
def test_explicit_root_is_required(root):
    with pytest.raises(ValueError):
        FS("a", "b", "c", root, NAMESPACE, session=FakeSession())


def test_namespace_requires_uuid():
    with pytest.raises(ValueError):
        FS("a", "b", "c", "private_folder", "production", session=FakeSession())


def test_instances_are_not_cached(session):
    options = dict(client_id="a", client_secret="b", refresh_token="c",
                   root_folder_id="private_folder", namespace=NAMESPACE, session=session)
    assert FS(**options) is not FS(**options)


def test_protocol_is_registered_without_replacing_other_protocols():
    from fsspec.registry import get_filesystem_class
    assert get_filesystem_class("fastegy_gdrive") is FS
    assert get_filesystem_class("file").protocol != "fastegy_gdrive"


def test_immutable_equal_content_is_idempotent_but_replacement_fails(fs, session):
    write(fs, data=b"first")
    write(fs, data=b"first")
    assert len(session.files) == 1
    with pytest.raises(FileExistsError):
        write(fs, data=b"other")
    assert fs.cat_file("photos/proof.jpg") == b"first"


def test_failed_close_does_not_reupload_when_closed_again(fs, session):
    write(fs, data=b"first")
    stream = fs.open("photos/proof.jpg", "wb")
    stream.write(b"other")
    with pytest.raises(FileExistsError):
        stream.close()
    assert stream.closed
    call_count = len(session.calls)
    stream.close()
    assert len(session.calls) == call_count


def test_checksum_matches_odoo_sha1_and_rejects_corrupted_bytes(fs, session):
    write(fs, data=b"odoo checksum contract")
    assert fs.checksum("photos/proof.jpg") == hashlib.sha1(b"odoo checksum contract").hexdigest()
    next(iter(session.files.values()))["_data"] = b"corrupted"
    with pytest.raises(RemoteIntegrityError):
        fs.checksum("photos/proof.jpg")


def test_duplicate_path_is_never_resolved_arbitrarily(fs, session):
    write(fs)
    duplicate = copy.deepcopy(next(iter(session.files.values())))
    duplicate["id"] = "duplicate_id"
    session.files["duplicate_id"] = duplicate
    with pytest.raises(DuplicateRemotePathError):
        fs.info("photos/proof.jpg")
    with pytest.raises(DuplicateRemotePathError):
        fs.exists("photos/proof.jpg")


def test_other_namespaces_are_invisible(fs, session):
    write(fs)
    other = FS("a", "b", "c", "private_folder", str(uuid.uuid4()), session=session)
    assert other.ls("", detail=False) == []
    assert not other.exists("photos/proof.jpg")
    with pytest.raises(FileNotFoundError):
        other.rm_file("photos/proof.jpg")
    assert not next(iter(session.files.values()))["trashed"]


def test_forged_description_fails_closed(fs, session):
    write(fs)
    next(iter(session.files.values()))["description"] = '{"path":"different.jpg"}'
    with pytest.raises(RemoteIntegrityError):
        fs.cat_file("photos/proof.jpg")


def test_readonly_blocks_every_mutation(fs, session):
    write(fs)
    readonly = FS("a", "b", "c", "private_folder", NAMESPACE, readonly=True, session=session)
    assert readonly.cat_file("photos/proof.jpg") == b"photo content"
    calls_before = len(session.calls)
    operations = [lambda: readonly.open("new", "wb"), lambda: readonly.makedirs("folder"),
                  lambda: readonly.cp_file("photos/proof.jpg", "copy"),
                  lambda: readonly.mv("photos/proof.jpg", "move"),
                  lambda: readonly.rm_file("photos/proof.jpg")]
    for operation in operations:
        with pytest.raises(PermissionError):
            operation()
    assert len(session.calls) == calls_before


def test_oversize_write_and_failed_context_do_not_upload(fs, session):
    fs.max_file_bytes = 4
    with pytest.raises(ValueError):
        write(fs, data=b"too big")
    with pytest.raises(RuntimeError):
        with fs.open("failed", "wb") as stream:
            stream.write(b"okay")
            raise RuntimeError("transaction body failed")
    assert not session.files
    assert not session.calls


def test_download_checks_both_size_and_checksum(fs, session):
    write(fs, data=b"safe")
    next(iter(session.files.values()))["_data"] = b"evil"
    with pytest.raises(RemoteIntegrityError):
        fs.cat_file("photos/proof.jpg")


def test_download_limit_even_if_metadata_lies(fs, session):
    write(fs, data=b"safe")
    fs.client.max_file_bytes = 5
    next(iter(session.files.values()))["_data"] = b"overflow"
    with pytest.raises(ValueError):
        fs.cat_file("photos/proof.jpg")


def test_interrupted_download_is_explicit_error(fs, session):
    write(fs)
    session.break_download = True
    with pytest.raises(DriveBackendError, match="interrupted"):
        fs.cat_file("photos/proof.jpg")


def test_get_retries_are_bounded_and_errors_sanitized(fs, session):
    session.failures[("GET", API)] = [FakeResponse(503, {"secret": "refresh-token"}) for _ in range(3)]
    with pytest.raises(DriveBackendError) as error:
        fs.exists("missing")
    assert "refresh-token" not in str(error.value)
    assert sum(method == "GET" and url == API for method, url, _ in session.calls) == 3


def test_permissions_are_not_silently_treated_as_missing(fs, session):
    session.failures[("GET", API)] = [FakeResponse(403)]
    with pytest.raises(PermissionError):
        fs.exists("private")


def test_expired_token_refreshes_once_on_401(fs, session):
    session.failures[("GET", API)] = [FakeResponse(401)]
    assert not fs.exists("missing")
    assert sum(url == TOKEN for _, url, _ in session.calls) == 2


def test_rejected_refresh_does_not_call_drive(fs, session):
    session.failures[("POST", TOKEN)] = [FakeResponse(400, {"error": "secret text"})]
    with pytest.raises(PermissionError) as error:
        fs.exists("missing")
    assert "secret text" not in str(error.value)
    assert len(session.calls) == 1


def test_ambiguous_upload_retries_same_allocated_id_and_verifies(fs, session):
    session.timeout_after_create = True
    write(fs)
    assert len(session.files) == 1
    assert sum(url == API + "/generateIds" for _, url, _ in session.calls) == 1
    uploads = [kwargs for method, url, kwargs in session.calls if method == "POST" and url == UPLOAD]
    assert len(uploads) == 2
    assert uploads[0]["data"] == uploads[1]["data"]
    assert uploads[0]["headers"]["Content-Type"] == uploads[1]["headers"]["Content-Type"]


def test_copy_and_move_keep_content_and_detect_destination_conflict(fs, session):
    write(fs)
    original_id = fs.info("photos/proof.jpg")["id"]
    fs.copy("photos/proof.jpg", "copy.jpg")
    with pytest.raises(FileExistsError):
        fs.mv("photos/proof.jpg", "copy.jpg")
    fs.rename("photos/proof.jpg", "moved.jpg")
    assert not fs.exists("photos/proof.jpg")
    assert fs.info("moved.jpg")["id"] == original_id
    assert fs.cat_file("moved.jpg") == fs.cat_file("copy.jpg")


def test_remove_trashes_only_and_never_deletes_root(fs, session):
    write(fs)
    fs.rm_file("photos/proof.jpg")
    assert not fs.exists("photos/proof.jpg")
    assert next(iter(session.files.values()))["trashed"]
    assert not any(method == "DELETE" for method, _, _ in session.calls)
    with pytest.raises(PermissionError):
        fs.rm_file("/")


def test_copy_and_move_cannot_replace_root(fs):
    write(fs)
    with pytest.raises(IsADirectoryError):
        fs.cp_file("photos/proof.jpg", "/")
    with pytest.raises(IsADirectoryError):
        fs.mv("photos/proof.jpg", "/")
    assert fs.exists("photos/proof.jpg")


def test_listing_follows_pagination(fs, session):
    write(fs, "a.pdf", b"a")
    write(fs, "b.pdf", b"b")
    session.page_size = 1
    assert fs.ls("", detail=False) == ["a.pdf", "b.pdf"]


@pytest.mark.parametrize("path", ["../escape", "a/../escape", "bad\\path", "bad\x00path"])
def test_paths_cannot_escape_namespace(fs, path):
    with pytest.raises(ValueError):
        fs.open(path, "wb")


def test_roundtrip_does_not_open_persistent_local_files(fs, monkeypatch):
    def reject_local_open(*args, **kwargs):
        raise AssertionError("Backend attempted local file IO")
    monkeypatch.setattr("builtins.open", reject_local_open)
    write(fs, data=b"memory-only")
    assert fs.cat_file("photos/proof.jpg") == b"memory-only"
