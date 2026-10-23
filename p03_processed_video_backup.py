"""Best-effort daily Drive backup for the processed video registry."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BACKUP_DIR = Path("_p03_state") / "processed_videos"
BACKUP_NAME_RE = re.compile(r"^processed_videos_(\d{8})\.csv$")
LOG = logging.getLogger(__name__)


def backup_processed_registry(data_root: str | Path, *, drive_root: str | Path | None = None,
                              today: date | None = None) -> Path | None:
    """Copy the registry once per KST date, verify content, and retain 14 copies.

    This is intentionally best-effort: a missing registry, Drive mount, or failed
    readback logs a warning and never interrupts the video batch.
    """
    try:
        from scripts.drive_yt_summary.config import discover_yt_summary_root

        source = Path(data_root) / "processed_videos.csv"
        if not source.is_file():
            raise FileNotFoundError(f"processed registry not found: {source}")
        content = source.read_bytes()
        stamp = (today or datetime.now(ZoneInfo("Asia/Seoul")).date()).strftime("%Y%m%d")
        backend = (os.getenv("P03_DRIVE_BACKEND") or
                   ("api" if sys.platform.startswith("linux") else "desktop")).strip().lower()
        if drive_root is None and backend == "api":
            return _backup_via_drive_api(content, stamp)
        root = Path(drive_root).expanduser() if drive_root is not None else discover_yt_summary_root()
        if root is None or not root.is_dir():
            raise FileNotFoundError("YT_summary Drive root not found")
        folder = root / BACKUP_DIR
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"processed_videos_{stamp}.csv"
        expected = hashlib.sha256(content).hexdigest()

        # A verified same-day copy is already the unique daily backup.
        if destination.is_file():
            try:
                if hashlib.sha256(destination.read_bytes()).hexdigest() == expected:
                    _prune(folder, destination)
                    return destination
            except OSError:
                pass

        fd, tmp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=folder)
        os.close(fd)
        try:
            Path(tmp_name).write_bytes(content)
            os.replace(tmp_name, destination)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        observed = hashlib.sha256(destination.read_bytes()).hexdigest()
        if observed != expected:
            raise OSError("processed registry Drive backup SHA-256 readback mismatch")
        _prune(folder, destination)
        return destination
    except Exception as exc:
        LOG.warning("P03_PROCESSED_REGISTRY_BACKUP_FAILED: %s: %s", type(exc).__name__, exc)
        return None


def _backup_via_drive_api(content: bytes, stamp: str) -> Path:
    """Store and verify a daily backup under the existing Drive summary root."""
    import io

    from googleapiclient.http import MediaIoBaseUpload
    from drive_api_sink import DriveApiSink

    sink = DriveApiSink()
    files = sink._drive.files()

    def child_folder(parent: str, name: str) -> str:
        escaped = name.replace("'", "\\'")
        response = files.list(
            q=f"'{parent}' in parents and name='{escaped}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            pageSize=100, fields="files(id,name,parents,mimeType)", supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        matches = [item for item in response.get("files", [])
                   if item.get("name") == name and parent in item.get("parents", [])
                   and item.get("mimeType") == "application/vnd.google-apps.folder"]
        if matches:
            return matches[0]["id"]
        return files.create(
            body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]},
            fields="id", supportsAllDrives=True,
        ).execute()["id"]

    folder_id = child_folder(child_folder(sink.summary_folder_id, "_p03_state"), "processed_videos")
    name = f"processed_videos_{stamp}.csv"
    escaped = name.replace("'", "\\'")
    listed = files.list(
        q=f"'{folder_id}' in parents and name='{escaped}' and trashed=false",
        pageSize=100, fields="files(id,name,parents,mimeType)", supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    same_day = next((item for item in listed if item.get("name") == name and
                     folder_id in item.get("parents", [])), None)
    expected = hashlib.sha256(content).hexdigest()
    observed = None
    if same_day:
        file_id = same_day["id"]
        try:
            observed = files.get_media(fileId=file_id, supportsAllDrives=True).execute()
        except Exception:
            observed = None
    if not same_day or not isinstance(observed, bytes) or hashlib.sha256(observed).hexdigest() != expected:
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype="text/csv", resumable=False)
        if same_day:
            files.update(fileId=file_id, body={"name": name}, media_body=media,
                         fields="id", supportsAllDrives=True).execute()
        else:
            file_id = files.create(
                body={"name": name, "parents": [folder_id]}, media_body=media,
                fields="id", supportsAllDrives=True,
            ).execute()["id"]
    observed = files.get_media(fileId=file_id, supportsAllDrives=True).execute()
    if not isinstance(observed, bytes) or hashlib.sha256(observed).hexdigest() != expected:
        raise OSError("processed registry Drive API SHA-256 readback mismatch")

    all_children = []
    page_token = None
    while True:
        kwargs = {"q": f"'{folder_id}' in parents and trashed=false", "pageSize": 1000,
                  "fields": "nextPageToken,files(id,name,parents,mimeType)", "supportsAllDrives": True,
                  "includeItemsFromAllDrives": True}
        if page_token:
            kwargs["pageToken"] = page_token
        response = files.list(**kwargs).execute()
        all_children.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    owned = [item for item in all_children if folder_id in item.get("parents", []) and
             item.get("mimeType") != "application/vnd.google-apps.folder" and
             BACKUP_NAME_RE.fullmatch(str(item.get("name", "")))]
    owned.sort(key=lambda item: str(item.get("name", "")), reverse=True)
    if not any(item.get("id") == file_id for item in owned):
        raise OSError("processed registry backup missing from Drive retention readback")
    for old in owned[14:]:
        if old.get("id") != file_id:
            files.delete(fileId=old["id"], supportsAllDrives=True).execute()
    return Path(BACKUP_DIR) / name


def _prune(folder: Path, newest: Path) -> None:
    """Keep the newest 14 exact-name registry backups in this folder only."""
    backups = []
    for path in folder.iterdir():
        match = BACKUP_NAME_RE.fullmatch(path.name)
        if match and path.is_file():
            backups.append((match.group(1), path))
    backups.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    keep = {path for _, path in backups[:14]}
    keep.add(newest)
    for _, path in backups:
        if path not in keep:
            path.unlink()
