#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Environment and path resolution for YT_summary filesystem sync (SUE-401)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from scripts.note_catalog_utils import resolve_paths

DRIVE_CLOUD_STORAGE = Path.home() / "Library" / "CloudStorage"


@dataclass(frozen=True)
class DriveSyncConfig:
    base_path: str
    work_path: str
    data_root: str
    md_root: str
    sync_root: Path
    source_dir: Path
    legacy_dir: Path
    enabled: bool
    state_path: Path


class DriveSyncConfigError(Exception):
    """Configuration or path resolution failure."""


def _env_bool(name: str, default: bool = True) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def discover_yt_summary_root() -> Optional[Path]:
    """Find Google Drive Desktop local YT_summary folder (no hard-coded account path)."""
    raw = (os.getenv("P03_DRIVE_SYNC_ROOT") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_dir() else None

    if not DRIVE_CLOUD_STORAGE.is_dir():
        return None

    rel_candidates = [
        Path("내 드라이브") / "PJT" / "YT_summary",
        Path("My Drive") / "PJT" / "YT_summary",
        Path("내 드라이브") / "YT_summary",
        Path("My Drive") / "YT_summary",
    ]
    for mount in sorted(DRIVE_CLOUD_STORAGE.glob("GoogleDrive-*")):
        if not mount.is_dir():
            continue
        for rel in rel_candidates:
            candidate = mount / rel
            if candidate.is_dir():
                return candidate
        # Fallback: shallow search for YT_summary under mount (max depth 4)
        try:
            for path in mount.rglob("YT_summary"):
                if path.is_dir() and path.name == "YT_summary":
                    return path
        except OSError:
            continue
    return None


def verify_sync_root(sync_root: Path) -> None:
    if not sync_root.is_dir():
        raise DriveSyncConfigError(
            f"Drive sync root not found or not a directory: {sync_root}"
        )
    if not os.access(sync_root, os.W_OK):
        raise DriveSyncConfigError(
            f"Drive sync root is not writable (Google Drive Desktop mounted?): {sync_root}"
        )


def load_config(
    base_path: Optional[str] = None,
    work_path: Optional[str] = None,
    md_path: Optional[str] = None,
    sync_root: Optional[str] = None,
) -> DriveSyncConfig:
    base, work, data_root, md_root = resolve_paths(base_path, work_path, md_path)

    if sync_root:
        root = Path(sync_root).expanduser()
    else:
        root = discover_yt_summary_root()
    if root is None:
        raise DriveSyncConfigError(
            "YT_summary local path not found. Set P03_DRIVE_SYNC_ROOT to the "
            "Google Drive Desktop folder (e.g. .../PJT/YT_summary)."
        )

    from scripts.note_catalog_utils import index_dir

    state_path = Path(index_dir(work, data_root)) / "drive_yt_summary_sync_state.json"

    return DriveSyncConfig(
        base_path=base,
        work_path=work,
        data_root=data_root,
        md_root=md_root,
        sync_root=root,
        source_dir=root / "source",
        legacy_dir=root / "legacy",
        enabled=_env_bool("P03_DRIVE_SYNC_ENABLED", True),
        state_path=state_path,
    )
