"""Google Drive backend; importable without importing Odoo."""

from fsspec.registry import register_implementation, registry

from .drive_client import (
    DriveBackendError,
    DriveClient,
    DuplicateRemotePathError,
    GoogleDriveClient,
    RemoteIntegrityError,
)
from .filesystem import FastEgyGoogleDriveFileSystem

if FastEgyGoogleDriveFileSystem.protocol not in registry:
    register_implementation(
        FastEgyGoogleDriveFileSystem.protocol, FastEgyGoogleDriveFileSystem
    )

__all__ = [
    "DriveBackendError",
    "DriveClient",
    "DuplicateRemotePathError",
    "FastEgyGoogleDriveFileSystem",
    "GoogleDriveClient",
    "RemoteIntegrityError",
]
