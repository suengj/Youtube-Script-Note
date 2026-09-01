#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Google Drive API wrapper for YT_summary sync."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from .credentials import DriveCredentialResolution, resolve_drive_credentials

FOLDER_MIME = "application/vnd.google-apps.folder"
MARKDOWN_MIME = "text/markdown"


class DriveAccessError(Exception):
    """Drive authentication or permission failure."""


@dataclass(frozen=True)
class DriveFolder:
    id: str
    name: str


class DriveYtSummaryClient:
    def __init__(
        self,
        service_account_file: Path,
        yt_summary_folder_id: str,
        auth: Optional[DriveCredentialResolution] = None,
    ) -> None:
        self._yt_summary_folder_id = yt_summary_folder_id
        try:
            self._auth = auth or resolve_drive_credentials(service_account_file)
            self._drive = build(
                "drive",
                "v3",
                credentials=self._auth.credentials,
                cache_discovery=False,
            )
        except Exception as exc:
            raise DriveAccessError(f"Failed to load Drive credentials: {exc}") from exc

    @property
    def auth_source(self) -> str:
        return self._auth.source

    @property
    def service_account_email(self) -> str:
        return self._auth.service_account_email or ""

    def verify_root_folder(self) -> DriveFolder:
        try:
            meta = self._drive.files().get(
                fileId=self._yt_summary_folder_id,
                fields="id,name",
                supportsAllDrives=True,
            ).execute()
        except HttpError as exc:
            hint = "Ensure OAuth token or service account can access YT_summary"
            if self._auth.source == "service_account" and self.service_account_email:
                hint = f"Share YT_summary with service account {self.service_account_email}"
            raise DriveAccessError(
                f"Cannot access YT_summary folder id={self._yt_summary_folder_id}: {exc}. {hint}."
            ) from exc
        return DriveFolder(id=meta["id"], name=meta.get("name", ""))

    def list_children(self, parent_id: str) -> List[DriveFolder]:
        resp = self._drive.files().list(
            q=f"'{parent_id}' in parents and trashed=false",
            fields="files(id,name,mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        out: List[DriveFolder] = []
        for item in resp.get("files", []):
            if item.get("mimeType") == FOLDER_MIME:
                out.append(DriveFolder(id=item["id"], name=item.get("name", "")))
        return out

    def list_files(self, parent_id: str) -> List[dict[str, Any]]:
        resp = self._drive.files().list(
            q=f"'{parent_id}' in parents and trashed=false",
            fields="files(id,name,mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        return list(resp.get("files", []))

    def find_child_folder(self, parent_id: str, name: str) -> Optional[DriveFolder]:
        for child in self.list_children(parent_id):
            if child.name == name:
                return child
        return None

    def create_folder(self, parent_id: str, name: str) -> DriveFolder:
        body = {
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id],
        }
        created = self._drive.files().create(
            body=body,
            fields="id,name",
            supportsAllDrives=True,
        ).execute()
        return DriveFolder(id=created["id"], name=created.get("name", name))

    def get_or_create_folder(self, parent_id: str, name: str) -> DriveFolder:
        existing = self.find_child_folder(parent_id, name)
        if existing:
            return existing
        return self.create_folder(parent_id, name)

    def move_item(self, file_id: str, new_parent_id: str, remove_parent_id: str) -> None:
        self._drive.files().update(
            fileId=file_id,
            addParents=new_parent_id,
            removeParents=remove_parent_id,
            fields="id",
            supportsAllDrives=True,
        ).execute()

    def _wrap_quota_error(self, exc: Exception) -> DriveAccessError:
        if self._auth.source == "service_account":
            detail = (
                "Service accounts cannot create files in My Drive (no storage quota). "
                "Share YT_summary on a Shared Drive with the service account, or grant "
                "domain-wide delegation — not installed-app OAuth (SUE-401 authority)."
            )
            return DriveAccessError(f"{detail}; underlying: {exc}")
        return DriveAccessError(str(exc))

    def upload_markdown(self, parent_id: str, name: str, content: str) -> str:
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")),
            mimetype=MARKDOWN_MIME,
            resumable=False,
        )
        try:
            created = self._drive.files().create(
                body={"name": name, "parents": [parent_id]},
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            ).execute()
            return created["id"]
        except HttpError as exc:
            raise self._wrap_quota_error(exc) from exc

    def update_markdown(self, file_id: str, content: str) -> None:
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")),
            mimetype=MARKDOWN_MIME,
            resumable=False,
        )
        try:
            self._drive.files().update(
                fileId=file_id,
                media_body=media,
                supportsAllDrives=True,
            ).execute()
        except HttpError as exc:
            raise self._wrap_quota_error(exc) from exc

    def upload_or_update_text_file(
        self,
        parent_id: str,
        name: str,
        content: str,
        file_id: Optional[str],
    ) -> str:
        if file_id:
            self.update_markdown(file_id, content)
            return file_id
        return self.upload_markdown(parent_id, name, content)
