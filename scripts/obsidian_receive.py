#!/usr/bin/env python3
"""Receive selected-channel P03 summaries from Drive into the Mac Obsidian vault."""
from __future__ import annotations

import csv
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterable, Optional

from scripts.note_catalog_utils import extract_channel_from_filename
from filename_utils import fit_filename

SOURCE_FOLDER_ID = "1777fpf3nO2-4ISKAt_3D1ipiwgJddYY-"
TOKEN_ENV = "P03_DRIVE_TOKEN_FILE"
CHANNEL_CSV_ENV = "P03_CHANNEL_CSV"
STATE_NAME = "p03_obsidian_receive_state.json"
MAX_LEDGER_BYTES = 10 * 1024 * 1024
MAX_NOTE_BYTES = 128 * 1024 * 1024
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class ReceiveError(RuntimeError):
    """Typed, safe-to-report receiver failure."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _channel_csv_path() -> Path:
    explicit = (os.getenv(CHANNEL_CSV_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_root = (os.getenv("DATA_ROOT") or "").strip()
    if data_root:
        return Path(data_root).expanduser() / "channel_df.csv"
    from config import resolve_data_root
    from pathlib import Path as _Path

    base = (os.getenv("BASE_PATH") or str(_Path(__file__).resolve().parents[1])).strip()
    return Path(resolve_data_root(base, os.getenv("WORK_PATH") or None)) / "channel_df.csv"


def _state_path() -> Path:
    state_dir = (os.getenv("P03_STATE_DIR") or "").strip()
    if state_dir:
        return Path(state_dir).expanduser() / STATE_NAME
    work = (os.getenv("WORK_PATH") or "").strip()
    if work:
        return Path(work).expanduser() / "index" / STATE_NAME
    data_root = (os.getenv("DATA_ROOT") or "").strip()
    if data_root:
        return Path(data_root).expanduser() / "index" / STATE_NAME
    from config import resolve_data_root

    base = (os.getenv("BASE_PATH") or str(Path(__file__).resolve().parents[1])).strip()
    return Path(resolve_data_root(base, None)) / "index" / STATE_NAME


def _selected_channels(csv_path: Path) -> Dict[str, Dict[str, set[str]]]:
    if not csv_path.is_file():
        raise ReceiveError("channel CSV is missing or not a regular file")
    if csv_path.stat().st_size > 4 * 1024 * 1024:
        raise ReceiveError("channel CSV exceeds 4 MiB")
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not {x.lower() for x in reader.fieldnames} >= {"obsidian", "channel_id"}:
                raise ReceiveError("channel CSV requires channel_id and obsidian columns")
            fields = {x.lower(): x for x in reader.fieldnames}
            result: Dict[str, Dict[str, set[str]]] = {}
            for row in reader:
                if not isinstance(row, dict) or None in row:
                    raise ReceiveError("channel CSV contains a malformed row")
                if (row.get(fields["obsidian"]) or "").strip().lower() != "recording":
                    continue
                cid = (row.get(fields["channel_id"]) or "").strip()
                if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", cid):
                    raise ReceiveError("selected channel has a malformed channel_id")
                aliases = {cid}
                for key in ("channel_name", "usage_channel"):
                    if key in fields:
                        val = (row.get(fields[key]) or "").strip()
                        if val:
                            aliases.add(val)
                result[cid] = {"aliases": aliases}
            return result
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReceiveError(f"cannot read channel CSV: {type(exc).__name__}") from exc


def _load_ledger(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LEDGER_BYTES:
            raise ReceiveError("receive ledger is unsafe or exceeds 10 MiB")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ReceiveError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiveError(f"cannot read receive ledger: {type(exc).__name__}") from exc
    if not isinstance(raw, dict) or set(raw) != {"version", "files"} or raw["version"] != 1 or not isinstance(raw["files"], dict):
        raise ReceiveError("receive ledger has invalid structure")
    for drive_id, entry in raw["files"].items():
        if (not isinstance(drive_id, str) or not drive_id or not isinstance(entry, dict) or
                set(entry) != {"md5", "local_path", "local_sha256"} or
                not re.fullmatch(r"[0-9a-f]{32}", str(entry["md5"])) or
                not isinstance(entry["local_path"], str) or not entry["local_path"] or
                not re.fullmatch(r"[0-9a-f]{64}", str(entry["local_sha256"]))):
            raise ReceiveError("receive ledger contains a malformed entry")
    return raw["files"]


def _atomic_write(vault: Path, name: str, data: bytes, *, replace: bool = True) -> bool:
    """Atomic sibling replace using a pinned directory fd (safe from symlink swaps)."""
    if not re.fullmatch(r"[^/\\\x00]+\.md", name) or name in {".md", "..md"} or len(name.encode("utf-8")) > 240:
        raise ReceiveError("Drive returned an unsafe or oversized note name")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(vault, flags)
    except OSError as exc:
        raise ReceiveError("vault directory cannot be safely opened") from exc
    temp_name = f".p03-receive-{os.getpid()}-{next(tempfile._get_candidate_names())}.tmp"
    try:
        for attempt in range(3):
            fd = -1
            try:
                fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dir_fd)
                with os.fdopen(fd, "wb") as out:
                    fd = -1
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                if replace:
                    os.replace(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                else:
                    # A hard link installs the completed sibling atomically and
                    # fails if any file appeared at the destination meanwhile.
                    os.link(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
                    os.unlink(temp_name, dir_fd=dir_fd)
                return True
            except OSError as exc:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(temp_name, dir_fd=dir_fd)
                except OSError:
                    pass
                if not replace and exc.errno == errno.EEXIST:
                    return False
                if exc.errno == 11 and attempt < 2:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise ReceiveError(f"atomic vault write failed: {type(exc).__name__}") from exc
    finally:
        os.close(dir_fd)


def _candidate_name(vault: Path, name: str, video_id: str, drive_md5: str, data: bytes) -> str:
    """Install the candidate at a never-before-used basename, without clobbering races."""
    stem = name[:-3]
    suffix = f".update-candidate-{drive_md5[:8]}"
    number = 1
    while True:
        numbered = suffix if number == 1 else f"{suffix}-{number}"
        target = fit_filename(f"{stem}{numbered}.md", video_id, max_bytes=240)
        if _atomic_write(vault, target, data, replace=False):
            return target
        number += 1


def _local_hash(vault: Path, name: str) -> Optional[str]:
    dir_fd = os.open(vault, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ReceiveError("vault note is not a safe regular file") from exc
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ReceiveError("vault note is not a regular file")
            return _sha(stream.read(MAX_NOTE_BYTES + 1))
    finally:
        os.close(dir_fd)


def _drive_service():
    token_path = (os.getenv(TOKEN_ENV) or "").strip()
    if not token_path:
        raise ReceiveError(f"{TOKEN_ENV} must point to the authorized user token")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(Path(token_path).expanduser()), ["https://www.googleapis.com/auth/drive"])
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds.valid:
            raise ReceiveError("Drive OAuth credentials are invalid")
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except ReceiveError:
        raise
    except Exception as exc:
        raise ReceiveError(f"Drive service setup failed: {type(exc).__name__}") from exc


def _list_summaries(service) -> Iterable[dict]:
    token = None
    while True:
        try:
            response = service.files().list(
                q=f"'{SOURCE_FOLDER_ID}' in parents and trashed=false", spaces="drive", pageSize=1000,
                pageToken=token, fields="nextPageToken,files(id,name,mimeType,parents,appProperties,md5Checksum,size)",
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
        except Exception as exc:
            raise ReceiveError(f"Drive list failed: {type(exc).__name__}") from exc
        if not isinstance(response, dict) or not isinstance(response.get("files", []), list):
            raise ReceiveError("Drive returned malformed list data")
        for item in response.get("files", []):
            if not isinstance(item, dict):
                raise ReceiveError("Drive returned a malformed file record")
            yield item
        token = response.get("nextPageToken")
        if not token:
            return


def receive(*, service=None, channel_csv: Optional[Path] = None, vault: Optional[Path] = None,
            state_path: Optional[Path] = None, max_count: Optional[int] = None) -> dict:
    selected = _selected_channels(channel_csv or _channel_csv_path())
    vault_root = Path(vault or (os.getenv("OUTPUT_MD_PATH") or "")).expanduser()
    if not str(vault_root) or not vault_root.is_dir():
        raise ReceiveError("OUTPUT_MD_PATH must name an existing vault directory")
    vault_root = vault_root.resolve(strict=True)
    ledger_path = state_path or _state_path()
    ledger = _load_ledger(ledger_path)
    try:
        bound = max_count if max_count is not None else int(os.getenv("P03_OBSIDIAN_MAX", "50"))
    except ValueError as exc:
        raise ReceiveError("P03_OBSIDIAN_MAX must be an integer") from exc
    if bound < 1 or bound > 10000:
        raise ReceiveError("P03_OBSIDIAN_MAX must be between 1 and 10000")
    drive = service or _drive_service()
    result = {"received": 0, "updated": 0, "candidates": 0, "skipped": 0, "errors": 0}
    changed = False
    written = 0
    for item in _list_summaries(drive):
        if written >= bound:
            break
        props = item.get("appProperties")
        if (not isinstance(props, dict) or props.get("p03_kind") != "summary" or
                not isinstance(props.get("p03_video_id"), str) or not VIDEO_ID_RE.fullmatch(props["p03_video_id"])):
            continue
        parents = item.get("parents")
        if not isinstance(parents, list) or SOURCE_FOLDER_ID not in parents:
            continue
        cid = props.get("p03_channel_id")
        prefix = extract_channel_from_filename(str(item.get("name", "")))
        matched = [channel_id for channel_id, data in selected.items()
                   if (cid == channel_id if cid is not None else prefix in data["aliases"])]
        if len(matched) != 1:
            continue
        drive_id, name = item.get("id"), item.get("name")
        md5 = item.get("md5Checksum")
        if (not isinstance(drive_id, str) or not drive_id or not isinstance(md5, str) or
                not re.fullmatch(r"[0-9a-f]{32}", md5) or
                not isinstance(name, str) or props.get("p03_video_id") not in name):
            continue
        try:
            old = ledger.get(drive_id)
            if old:
                local_name = old["local_path"]
                if Path(local_name).name != local_name:
                    raise ReceiveError("receive ledger path is not a vault basename")
                current = _local_hash(vault_root, local_name)
                if old["md5"] == md5 and current == old["local_sha256"]:
                    result["skipped"] += 1
                    continue
                if current is None or current == old["local_sha256"]:
                    target_name = local_name
                    kind = "updated"
                else:
                    kind = "candidates"
            else:
                target_name = name
                current = _local_hash(vault_root, target_name)
                if current is not None:
                    # A note not written by this ledger is user-owned from the receiver's perspective.
                    kind = "candidates"
                else:
                    kind = "received"
            data = drive.files().get_media(fileId=drive_id).execute()
            if not isinstance(data, bytes) or len(data) > MAX_NOTE_BYTES or hashlib.md5(data).hexdigest() != md5:
                raise ReceiveError("Drive summary body failed size or MD5 verification")
            if kind == "candidates":
                target_name = _candidate_name(vault_root, name, props["p03_video_id"], md5, data)
            else:
                _atomic_write(vault_root, target_name, data)
            local_sha = _local_hash(vault_root, target_name)
            if local_sha != _sha(data):
                raise ReceiveError("vault readback SHA-256 verification failed")
            result[kind] += 1
            written += 1
            ledger[drive_id] = {"md5": md5, "local_path": target_name, "local_sha256": local_sha}
            changed = True
        except ReceiveError:
            raise
        except Exception as exc:
            # Preserve a typed refusal and never include token/API request contents.
            raise ReceiveError(f"Drive item processing failed: {type(exc).__name__}") from exc
    if changed:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "files": ledger}, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        _atomic_write_ledger(ledger_path, payload)
    return result


def _atomic_write_ledger(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".p03-receive-state-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def main() -> int:
    try:
        print(json.dumps(receive(), sort_keys=True))
        return 0
    except ReceiveError as exc:
        print(f"obsidian_receive: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
