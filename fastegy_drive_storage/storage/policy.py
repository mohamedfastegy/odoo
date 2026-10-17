"""Pure helpers shared by the Odoo models and contract tests."""
import ast
import fnmatch
import os
import re
import json
import stat
from pathlib import Path

from .drive_client import DriveAPIError, RemoteIntegrityError

PREFIX = "fernet:v1:"


def safe_name(value, default="file", limit=180):
    value = re.sub(r'[\x00-\x1f/\\]', "_", str(value or "")).strip(" .")
    return (value or default)[:limit]


def parse_domain(value):
    domain = ast.literal_eval(value or "[]")
    if not isinstance(domain, list):
        raise ValueError("A domain must be a literal list")
    return domain


def file_matches(mimetype, size, patterns="", minimum=0, maximum=0):
    return (size >= minimum and (not maximum or size <= maximum)
            and (not patterns or any(fnmatch.fnmatchcase(mimetype or "application/octet-stream", p.strip())
                                     for p in patterns.split(",") if p.strip())))


def encryption_keys():
    # المفاتيح خارج قاعدة البيانات والنسخ المصدرة من المستودع.
    from cryptography.fernet import Fernet, MultiFernet
    values = [k.strip() for k in os.environ.get("FASTEGY_DRIVE_SECRET_KEYS", "").split(",") if k.strip()]
    if not values:
        path = Path.home() / "data" / ".fastegy-drive" / "keys.json"
        if path.exists():
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Drive key file must be owned by the Odoo service user with mode 0600")
            values = json.loads(path.read_text())
            if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
                raise ValueError("Invalid external Drive key file")
    return MultiFernet([Fernet(k.encode("ascii")) for k in values]) if values else None


def seal(value, required=False):
    if not value or value.startswith(PREFIX):
        return value
    key = encryption_keys()
    if not key:
        if required:
            raise ValueError("Configure FASTEGY_DRIVE_SECRET_KEYS outside the database first")
        return value
    return PREFIX + key.encrypt(value.encode()).decode("ascii")


def unseal(value):
    if not value or not value.startswith(PREFIX):
        return value
    key = encryption_keys()
    if not key:
        raise ValueError("The external Drive encryption key is unavailable")
    try:
        return key.decrypt(value[len(PREFIX):].encode()).decode()
    except Exception:
        raise ValueError("The configured Drive encryption key cannot decrypt these credentials") from None


def classify_error(exc):
    if isinstance(exc, DriveAPIError):
        return exc.category, exc.transient
    if isinstance(exc, RemoteIntegrityError):
        return "integrity", False
    if isinstance(exc, FileNotFoundError):
        return "missing", False
    if isinstance(exc, PermissionError):
        return "authorization", False
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "network", True
    if isinstance(exc, ValueError):
        return "configuration_or_limit", False
    return "operation_failed", False
