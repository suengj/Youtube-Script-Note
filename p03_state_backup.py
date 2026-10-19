"""Bounded, verified backup of the small P03 batch state to Google Drive."""
from __future__ import annotations

import hashlib
import gzip
import io
import json
import logging
import os
import re
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from drive_api_sink import DriveUploadError
from scripts.note_catalog_utils import index_dir

KIND = "state_backup"
logger = logging.getLogger(__name__)
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
STATE_FILES = (
    ("data/channel_df.csv", "data"),
    ("data/crawl_yt_list.csv", "data"),
    ("data/output_df_new.csv", "data"),
    ("data/logs/llm_usage.jsonl", "data"),
    ("index/drive_yt_summary_sync_state.json", "base"),
)


def _read_regular(path: Path) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
        raise DriveUploadError(f"state input is not a bounded regular file: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise DriveUploadError(f"state input changed while opening: {path.name}")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise DriveUploadError(f"state input exceeds size limit: {path.name}")
            chunks.append(chunk)
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or total != after.st_size:
            raise DriveUploadError(f"state input changed while reading: {path.name}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def build_archive(data_root: str, base_path: str, *, now: datetime | None = None) -> tuple[str, bytes]:
    """Snapshot present canonical state files into a gzip tar with an exact manifest."""
    roots = {"data": Path(data_root), "base": Path(base_path)}
    # Keep the sync index lookup aligned with its producer, including WORK_PATH
    # and DATA_ROOT fallback behavior.
    work_path = os.getenv("WORK_PATH", "").strip()
    index_root = Path(index_dir(work_path, data_root))
    entries = []
    missing = []
    for relative, root_key in STATE_FILES:
        path = (roots[root_key] / relative.split("/", 1)[1] if root_key == "data"
                else index_root / Path(relative).name)
        try:
            path.lstat()
        except FileNotFoundError:
            missing.append(relative)
            logger.warning("P03_STATE_BACKUP_MISSING_FILE: %s", relative)
            continue
        expected_root = roots[root_key].resolve() if root_key == "data" else index_root.resolve()
        expected = expected_root / (relative.split("/", 1)[1] if root_key == "data" else Path(relative).name)
        if path.resolve() != expected:
            raise DriveUploadError(f"state path escaped its canonical root: {relative}")
        content = _read_regular(path)
        entries.append((relative, content))
    if not entries:
        raise DriveUploadError("no P03 state files were available to back up")
    manifest = {"schema_version": 1,
                "files": [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in entries],
                "missing": missing}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        info.mtime = 0
        archive.addfile(info, io.BytesIO(raw))
    result = buf.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise DriveUploadError("P03 state archive exceeds size limit")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"p03-state-{stamp}.tar.gz", result


def validate_archive(data: bytes) -> None:
    """Fail closed on malformed, partial, substituted, or oversized archive records."""
    allowed = {name for name, _ in STATE_FILES}
    if not isinstance(data, bytes) or len(data) > MAX_ARCHIVE_BYTES:
        raise DriveUploadError("P03 state archive is invalid or exceeds size limit")
    try:
        # tarfile's r:gz reader can return all tar members even when the gzip
        # trailer (CRC32/ISIZE) is truncated. Validate the gzip stream itself
        # before trusting the tar member and manifest checks below.
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as compressed:
            expanded = 0
            max_expanded = len(STATE_FILES) * MAX_FILE_BYTES + 1024 * 1024
            while chunk := compressed.read(1024 * 1024):
                expanded += len(chunk)
                if expanded > max_expanded:
                    raise DriveUploadError("P03 state archive exceeds expanded size limit")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            names = [item.name for item in members]
            if len(names) != len(set(names)) or "manifest.json" not in names or set(names) - {"manifest.json"} - allowed:
                raise DriveUploadError("state archive contains duplicate or unexpected members")
            records = {}
            for member in members:
                if not member.isfile() or member.size > MAX_FILE_BYTES + 16 * 1024:
                    raise DriveUploadError("state archive contains a non-file or oversized member")
                stream = archive.extractfile(member)
                content = stream.read(member.size + 1) if stream else b""
                if len(content) != member.size:
                    raise DriveUploadError("state archive member is partial")
                records[member.name] = content
            manifest = json.loads(records.pop("manifest.json").decode("utf-8"))
            listed = manifest.get("files")
            missing = manifest.get("missing")
            if (manifest.get("schema_version") != 1 or not isinstance(listed, list) or
                    len(listed) != len(records) or not isinstance(missing, list)):
                raise DriveUploadError("state archive manifest is incomplete")
            if (any(not isinstance(name, str) or name not in allowed for name in missing) or
                    len(missing) != len(set(missing)) or set(missing) & set(records)):
                raise DriveUploadError("state archive manifest missing-file list is malformed")
            seen = set()
            for item in listed:
                if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
                    raise DriveUploadError("state archive manifest record is malformed")
                name = item["path"]
                content = records.get(name)
                if (name in seen or content is None or item["size"] != len(content) or
                        not re.fullmatch(r"[0-9a-f]{64}", str(item["sha256"])) or
                        hashlib.sha256(content).hexdigest() != item["sha256"]):
                    raise DriveUploadError("state archive manifest verification failed")
                seen.add(name)
            if seen != set(records):
                raise DriveUploadError("state archive manifest omitted a member")
            if seen | set(missing) != allowed:
                raise DriveUploadError("state archive manifest omitted expected state")
    except DriveUploadError:
        raise
    except Exception as exc:
        raise DriveUploadError(f"malformed state archive: {type(exc).__name__}") from exc


def backup_state(sink, data_root: str, base_path: str) -> str:
    """Upload one archive, verify it, then prune only tagged P03 state backups."""
    name, data = build_archive(data_root, base_path)
    validate_archive(data)
    folder_id = sink._state_backup_folder()
    meta = sink._upload_state_backup(folder_id, name, data)
    sink._prune_state_backups(folder_id, keep=14, newest_id=meta["id"])
    return meta["id"]
