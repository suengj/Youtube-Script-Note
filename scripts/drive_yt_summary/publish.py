#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final-MD publish: Drive Desktop (canonical) + selective Obsidian mirror.

Responsibilities are split and failure-isolated:
  * staging   - local work/retry cache (WORK_PATH/output_md_staging); never the source of truth
  * Drive     - canonical copy under P03_DRIVE_SYNC_ROOT/source (Mac Drive Desktop mount)
  * mirror    - extra copy into OUTPUT_MD_PATH (vault), only for obsidian-marked channels

Local write only; cloud readback evidence is out of scope here (SUE-1299).
One writer per host: callers run inside the pipeline's single run; the lock only
serialises worker threads inside that process.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import DriveSyncConfigError, load_config, verify_sync_root
from .fs_transport import FilesystemSyncError, atomic_write_text, ensure_dir
from .state import SyncStateEntry, load_state, save_state

STAGING_DIRNAME = "output_md_staging"
_LOCK = threading.Lock()


def staging_root(work_path: Optional[str], base_path: str) -> Path:
    return Path((work_path or "").strip() or base_path) / STAGING_DIRNAME


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class PublishResult:
    drive_action: str = "skipped"   # created | updated | unchanged | disabled | failed
    drive_path: str = ""
    mirror_action: str = "skipped"  # mirrored | unchanged | skipped | failed
    mirror_path: str = ""
    staging_retained: bool = True
    revision: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def drive_ok(self) -> bool:
        return self.drive_action in {"created", "updated", "unchanged"}


class DriveDesktopAdapter:
    """Thin sync adapter: write to the local Drive Desktop folder, verify, record in state."""

    def __init__(self, sync_root: Path, source_dir: Path, state_path: Path) -> None:
        self.sync_root = sync_root
        self.source_dir = source_dir
        self.state_path = state_path

    def put(self, rel: str, drive_name: str, content: str, *, video_id: str = "", title: str = "") -> tuple:
        """Returns (action, dest_path, revision). Raises on failure."""
        digest = content_hash(content)
        verify_sync_root(self.sync_root)
        ensure_dir(self.source_dir)
        with _LOCK:
            state = load_state(self.state_path)
            prev = state.files.get(rel)
            dest = Path(prev.dest_path) if prev else self.source_dir / drive_name
            if prev and prev.content_hash == digest and dest.is_file():
                return "unchanged", str(dest), prev.revision
            action = "updated" if prev else "created"
            atomic_write_text(dest, content)
            if content_hash(dest.read_text(encoding="utf-8")) != digest:
                raise FilesystemSyncError(f"verify failed after write: {dest}")
            revision = (prev.revision + 1) if prev else 1
            state.files[rel] = SyncStateEntry(
                relative_path=rel,
                content_hash=digest,
                dest_path=str(dest),
                drive_name=drive_name,
                updated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                video_id=video_id or (prev.video_id if prev else ""),
                title=title or (prev.title if prev else ""),
                revision=revision,
            )
            save_state(self.state_path, state)
            return action, str(dest), revision


def _mirror_to_vault(md_root: str, rel: str, content: str) -> tuple:
    dest = Path(md_root) / rel
    if dest.is_file():
        try:
            if content_hash(dest.read_text(encoding="utf-8-sig")) == content_hash(content):
                return "unchanged", str(dest)
        except OSError:
            pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8-sig")
        os.replace(tmp, dest)
    except OSError:
        if tmp.exists():
            tmp.unlink()
        raise
    return "mirrored", str(dest)


def publish_final_md(
    *,
    rel_path: str,
    content: str,
    staging_path: Optional[str],
    md_root: str,
    mirror: bool,
    base_path: str,
    work_path: Optional[str] = None,
    sync_root: Optional[str] = None,
    video_id: str = "",
    title: str = "",
) -> PublishResult:
    """Drive write (canonical) then optional vault mirror; neither failure blocks the other.

    Staging is removed only after the Drive write is verified (or, when Drive sync is
    disabled, after the vault copy succeeds - legacy behaviour).
    """
    result = PublishResult()
    drive_name = Path(rel_path).name
    disabled = False

    try:
        config = load_config(base_path, work_path, md_root, sync_root=sync_root)
        if not config.enabled:
            disabled = True
            result.drive_action = "disabled"
        else:
            adapter = DriveDesktopAdapter(config.sync_root, config.source_dir, config.state_path)
            action, dest, revision = adapter.put(
                rel_path, drive_name, content, video_id=video_id, title=title
            )
            result.drive_action, result.drive_path, result.revision = action, dest, revision
    except (DriveSyncConfigError, FilesystemSyncError, OSError) as exc:
        result.drive_action = "failed"
        result.errors.append(f"drive: {exc}")
    except Exception as exc:  # never let storage break the pipeline
        result.drive_action = "failed"
        result.errors.append(f"drive: {type(exc).__name__}: {exc}")

    if mirror or disabled:
        try:
            result.mirror_action, result.mirror_path = _mirror_to_vault(md_root, rel_path, content)
        except Exception as exc:
            result.mirror_action = "failed"
            result.errors.append(f"mirror: {type(exc).__name__}: {exc}")

    safe_to_drop = result.drive_ok or (disabled and result.mirror_action in {"mirrored", "unchanged"})
    if safe_to_drop and staging_path:
        try:
            Path(staging_path).unlink(missing_ok=True)
            result.staging_retained = False
        except OSError:
            pass
    elif not staging_path:
        result.staging_retained = False
    return result


def flush_staging(
    *, md_root: str, base_path: str, work_path: Optional[str] = None, sync_root: Optional[str] = None
) -> List[PublishResult]:
    """Retry Drive publish for files left in staging (Drive only; mirror was attempted already)."""
    out: List[PublishResult] = []
    root = staging_root(work_path, base_path)
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*/*.md")):
        try:
            content = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        out.append(
            publish_final_md(
                rel_path=rel, content=content, staging_path=str(path), md_root=md_root,
                mirror=False, base_path=base_path, work_path=work_path, sync_root=sync_root,
            )
        )
    return out
