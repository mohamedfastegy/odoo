import copy
import hashlib
import importlib
import json
import re

import pytest
import requests
from cryptography.fernet import Fernet

from test_drive_backend import FakeResponse, FakeSession, FS, CLIENT_MODULE, API, UPLOAD, NAMESPACE

policy = importlib.import_module(CLIENT_MODULE.__package__ + ".policy")


class Response(FakeResponse):
    def __init__(self, *args, headers=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = headers or {}


class Transport(FakeSession):
    def __init__(self):
        super().__init__()
        self.uploads = {}
        self.lose_chunk_response = False
        self.chunk_writes = []
        self.drive_id = None

    def request(self, method, url, **kwargs):
        if self.failures.get((method, url)):
            return super().request(method, url, **kwargs)
        if method == "GET" and url == API:
            self.calls.append((method, url, copy.deepcopy(kwargs)))
            query = kwargs["params"]["q"]
            parent = re.search(r"'([^']+)' in parents", query)
            properties = re.findall(r"key='([^']+)' and value='([^']+)'", query)
            found = [self.metadata(f) for f in self.files.values() if not f.get("trashed")
                     and (not parent or parent.group(1) in f.get("parents", []))
                     and all(f.get("appProperties", {}).get(k) == v for k, v in properties)]
            return Response(payload={"files": found})
        if method == "POST" and url == API:
            self.calls.append((method, url, copy.deepcopy(kwargs)))
            self.next_id += 1
            metadata = dict(kwargs["json"], id="new_%s" % self.next_id, trashed=False)
            self.files[metadata["id"]] = metadata
            return Response(payload=self.metadata(metadata))
        if method == "PATCH" and url.startswith(API + "/"):
            self.calls.append((method, url, copy.deepcopy(kwargs)))
            metadata = self.files[url.rsplit("/", 1)[1]]
            metadata.update(kwargs["json"])
            if kwargs.get("params", {}).get("addParents"):
                metadata["parents"] = [kwargs["params"]["addParents"]]
            return Response(payload=self.metadata(metadata))
        if method == "POST" and url == UPLOAD and kwargs.get("params", {}).get("uploadType") == "resumable":
            self.calls.append((method, url, copy.deepcopy(kwargs)))
            location = UPLOAD + "?upload_id=private_session"
            self.uploads[location] = {"metadata": copy.deepcopy(kwargs["json"]), "data": bytearray()}
            return Response(headers={"Location": location})
        if method == "PUT" and url in self.uploads:
            self.calls.append((method, url, copy.deepcopy(kwargs)))
            upload = self.uploads[url]
            content_range = kwargs["headers"]["Content-Range"]
            if content_range.startswith("bytes */"):
                if upload["metadata"]["id"] in self.files:
                    return Response(payload=self.metadata(self.files[upload["metadata"]["id"]]))
                count = len(upload["data"])
                return Response(308, headers={"Range": "bytes=0-%s" % (count-1)} if count else {})
            start, end, total = map(int, re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range).groups())
            assert start == len(upload["data"])
            assert len(kwargs["data"]) == end-start+1
            self.chunk_writes.append((start, end))
            upload["data"].extend(kwargs["data"])
            if end+1 == total:
                metadata = upload["metadata"]
                metadata.update(size=str(total), md5Checksum=hashlib.md5(upload["data"]).hexdigest(),
                                _data=bytes(upload["data"]), trashed=False)
                self.files[metadata["id"]] = metadata
            if self.lose_chunk_response:
                self.lose_chunk_response = False
                raise requests.Timeout("response lost after chunk accepted")
            if end+1 == total:
                return Response(payload=self.metadata(metadata))
            return Response(308, headers={"Range": "bytes=0-%s" % end})
        if method == "GET" and url == API + "/private_folder":
            return Response(payload={"id": "private_folder", "name": "Root", "mimeType": "application/vnd.google-apps.folder",
                                     "driveId": self.drive_id, "trashed": False})
        if method == "GET" and kwargs.get("headers", {}).get("Range"):
            file = self.files[url.rsplit("/", 1)[1]]
            start, end = map(int, kwargs["headers"]["Range"].split("=")[1].split("-"))
            return Response(206, body=file["_data"][start:end+1], headers={
                "Content-Range": "bytes %s-%s/%s" % (start, end, len(file["_data"]))})
        return super().request(method, url, **kwargs)


@pytest.fixture
def remote():
    session = Transport()
    fs = FS("client", "secret", "refresh", "private_folder", NAMESPACE, session=session, nested_layout=True)
    return fs, session


def add(fs, path="blob", data=b"proof", mimetype="image/jpeg"):
    with fs.open(path, "wb", mimetype=mimetype) as stream:
        stream.write(data)
    return fs.client.lookup(path)


def test_mimetype_is_preserved_with_extensionless_key(remote):
    fs, session = remote
    assert add(fs)["mimeType"] == "image/jpeg"


def test_nested_folders_preserve_path_identity_and_bytes(remote):
    fs, session = remote
    old = add(fs)
    new = fs.client.update_presentation(old, "صورة التركيب.jpg", "image/jpeg", ["company_1", "installation", "record_4"])
    assert new["id"] == old["id"]
    assert new["name"] == "صورة التركيب.jpg"
    assert "private_folder" not in new["parents"]
    assert fs.client.lookup("blob")["id"] == old["id"]
    assert fs.client.download(new) == b"proof"


def test_existing_folder_is_reused(remote):
    fs, session = remote
    assert fs.client.ensure_folder("company_1") == fs.client.ensure_folder("company_1")
    assert len(session.files) == 1


def test_moving_outside_root_is_detected(remote):
    fs, session = remote
    metadata = add(fs)
    session.files[metadata["id"]]["parents"] = ["foreign_folder"]
    session.files["foreign_folder"] = {"id": "foreign_folder", "mimeType": "application/vnd.google-apps.folder", "parents": []}
    with pytest.raises(CLIENT_MODULE.RemoteIntegrityError):
        fs.client.lookup("blob")


def test_resumable_upload_recovers_lost_chunk_response(remote):
    fs, session = remote
    fs.client.resumable_threshold = 1
    fs.client.chunk_bytes = 262144
    session.lose_chunk_response = True
    data = b"x" * 700000
    metadata = add(fs, data=data)
    assert fs.client.download(metadata) == data
    assert session.chunk_writes == [(0, 262143), (262144, 524287), (524288, 699999)]
    assert len(session.files) == 1


def test_quota_is_not_retried_as_transient(remote):
    fs, session = remote
    session.failures[("GET", API + "/private_folder")] = [Response(403, payload={"error": {"errors": [{"reason": "storageQuotaExceeded"}]}})]
    with pytest.raises(CLIENT_MODULE.DriveAPIError) as error:
        fs.client.validate_root()
    assert error.value.category == "quota_exceeded"
    assert not error.value.transient


def test_403_rate_limit_is_retried(remote):
    fs, session = remote
    session.failures[("GET", API + "/private_folder")] = [Response(403, payload={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})]
    assert fs.client.validate_root()["id"] == "private_folder"


def test_stream_and_byte_range_have_exact_content(remote):
    fs, session = remote
    metadata = add(fs, data=b"0123456789")
    assert b"".join(fs.client.iter_download(metadata, 3)) == b"0123456789"
    assert b"".join(fs.client.iter_range(metadata, 2, 5)) == b"2345"


def test_stream_detects_changed_bytes(remote):
    fs, session = remote
    metadata = add(fs)
    session.files[metadata["id"]]["_data"] = b"wrong"
    with pytest.raises(CLIENT_MODULE.RemoteIntegrityError):
        b"".join(fs.client.iter_download(metadata))


def test_trash_and_restore_are_reversible(remote):
    fs, session = remote
    metadata = add(fs)
    fs.client.trash(metadata)
    assert session.files[metadata["id"]]["trashed"]
    fs.client.restore(metadata["id"])
    assert fs.client.download(fs.client.metadata(metadata["id"])) == b"proof"


def test_shared_drive_uses_explicit_corpus_and_verifies_root(remote):
    fs, session = remote
    fs.client.shared_drive_id = "company_drive"
    with pytest.raises(CLIENT_MODULE.RemoteIntegrityError):
        fs.client.validate_root()
    session.drive_id = "company_drive"
    fs.client.validate_root()
    add(fs)
    calls = [kwargs for method, url, kwargs in session.calls if url == API and method == "GET"]
    assert all(x["params"]["corpora"] == "drive" and x["params"]["driveId"] == "company_drive" for x in calls)


def test_existing_file_reference_has_no_duplicate_binary(remote):
    fs, session = remote
    target = {"id": "external_file", "name": "Existing.pdf", "size": "5", "mimeType": "application/pdf",
              "md5Checksum": hashlib.md5(b"proof").hexdigest(), "parents": ["foreign"], "_data": b"proof"}
    session.files[target["id"]] = target
    reference = fs.client.create_reference("reference_blob", target)
    assert len([x for x in session.files.values() if "_data" in x]) == 1
    assert fs.client.download(reference) == b"proof"
    fs.client.trash(reference)
    assert not target.get("trashed")


def test_reference_rejects_changed_original(remote):
    fs, session = remote
    target = {"id": "external", "name": "Proof.pdf", "size": "5", "mimeType": "application/pdf",
              "md5Checksum": hashlib.md5(b"proof").hexdigest(), "parents": ["foreign"], "_data": b"proof"}
    session.files[target["id"]] = target
    reference = fs.client.create_reference("ref", target)
    target["md5Checksum"] = "changed"
    with pytest.raises(CLIENT_MODULE.RemoteIntegrityError):
        fs.client.download(reference)


def test_secret_roundtrip_and_key_rotation(monkeypatch):
    first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("FASTEGY_DRIVE_SECRET_KEYS", first)
    encrypted = policy.seal("private-refresh-token", required=True)
    assert "private-refresh-token" not in encrypted
    assert policy.unseal(encrypted) == "private-refresh-token"
    monkeypatch.setenv("FASTEGY_DRIVE_SECRET_KEYS", second + "," + first)
    assert policy.unseal(encrypted) == "private-refresh-token"
    rewrapped = policy.seal(policy.unseal(encrypted), required=True)
    monkeypatch.setenv("FASTEGY_DRIVE_SECRET_KEYS", second)
    assert policy.unseal(rewrapped) == "private-refresh-token"


def test_missing_secret_key_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(policy.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("FASTEGY_DRIVE_SECRET_KEYS", Fernet.generate_key().decode())
    encrypted = policy.seal("secret", required=True)
    monkeypatch.delenv("FASTEGY_DRIVE_SECRET_KEYS")
    with pytest.raises(ValueError):
        policy.unseal(encrypted)
    with pytest.raises(ValueError):
        policy.seal("secret", required=True)


def test_external_key_file_requires_private_permissions(monkeypatch, tmp_path):
    monkeypatch.delenv("FASTEGY_DRIVE_SECRET_KEYS", raising=False)
    monkeypatch.setattr(policy.Path, "home", lambda: tmp_path)
    keyfile = tmp_path / "data" / ".fastegy-drive" / "keys.json"
    keyfile.parent.mkdir(parents=True)
    keyfile.write_text(json.dumps([Fernet.generate_key().decode()]))
    keyfile.chmod(0o644)
    with pytest.raises(ValueError):
        policy.seal("secret", required=True)
    keyfile.chmod(0o600)
    encrypted = policy.seal("secret", required=True)
    assert policy.unseal(encrypted) == "secret"


@pytest.mark.parametrize("domain", ["__import__('os').system('anything')", "{'name':'value'}", "lambda: 1"])
def test_rule_domain_has_no_executable_python(domain):
    with pytest.raises((ValueError, SyntaxError)):
        policy.parse_domain(domain)


def test_rules_and_names_are_bounded():
    assert policy.file_matches("image/jpeg", 500, "image/*", 100, 1000)
    assert not policy.file_matches("application/pdf", 500, "image/*")
    assert not policy.file_matches("image/jpeg", 2000, "image/*", 0, 1000)
    assert policy.safe_name("../unsafe\\name\n") == "_unsafe_name_"


def test_metadata_audit_page_can_resume_without_loading_all_files():
    session = FakeSession()
    fs = FS("client", "secret", "refresh", "private_folder", NAMESPACE, session=session)
    add(fs, "first", b"one")
    add(fs, "second", b"two")
    session.page_size = 1
    page, token = fs.client.list_file_page(page_size=1)
    assert [fs.client.file_path(item) for item in page] == ["first"]
    assert token
    page, token = fs.client.list_file_page(token=token, page_size=1)
    assert [fs.client.file_path(item) for item in page] == ["second"]
    assert not token


def test_incomplete_remote_inventory_cannot_appear_successful(remote):
    fs, session = remote
    session.failures[("GET", API)] = [Response(payload={"files": [], "incompleteSearch": True})]
    with pytest.raises(CLIENT_MODULE.DriveBackendError, match="incomplete"):
        fs.client.list_file_page()
    params = [kwargs["params"] for method, url, kwargs in session.calls if method == "GET" and url == API][-1]
    assert "incompleteSearch" in params["fields"]


def test_stable_business_folders_move_once_and_rename_without_duplicates(remote):
    fs, session = remote
    old = add(fs)
    folders = ["company_2", {"key": "business:app:installation", "name": "التركيبات"},
               {"key": "business:record:12", "name": "FE-12"},
               {"key": "business:category:images", "name": "صور التركيب"}]
    first = fs.client.update_presentation(old, "proof.jpg", "image/jpeg", folders)
    count = len(session.files)
    folders[2]["name"] = "FE-12 revised"
    second = fs.client.update_presentation(first, "proof.jpg", "image/jpeg", folders)
    third = fs.client.update_presentation(second, "proof.jpg", "image/jpeg", folders)
    assert len(session.files) == count
    assert old["id"] == first["id"] == second["id"] == third["id"]
    assert first["parents"] == second["parents"] == third["parents"]
    assert fs.client.lookup("blob")["id"] == old["id"]
    assert fs.client.download(third) == b"proof"
    assert any(f["name"] == "FE-12 revised" for f in session.files.values())
