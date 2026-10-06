#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Filesystem transport for YT_summary sync (atomic writes, no Drive API)."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path


def short_tmp_path(path: Path) -> Path:
    """Sibling temp path whose name is short regardless of how long ``path.name`` is.

    ``name + '.tmp'`` would overflow the 255-byte limit for a name already at the cap.
    """
    digest = hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:12]
    return path.with_name(f".{digest}.{os.getpid()}.tmp")


class FilesystemSyncError(Exception):
    """Local Drive Desktop filesystem operation failure."""


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = short_tmp_path(path)
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise FilesystemSyncError(f"atomic write failed for {path}: {exc}") from exc


def copy_or_update_file(src: Path, dest: Path, content: str) -> str:
    """Copy new file or overwrite existing. Returns action: created | updated."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.is_file():
        atomic_write_text(dest, content)
        return "created"
    atomic_write_text(dest, content)
    return "updated"


def ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FilesystemSyncError(f"cannot create directory {path}: {exc}") from exc


def move_dir(src: Path, dest: Path) -> None:
    if not src.is_dir():
        raise FilesystemSyncError(f"source directory missing: {src}")
    if dest.exists():
        raise FilesystemSyncError(f"destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dest))
    except OSError as exc:
        raise FilesystemSyncError(f"move failed {src} → {dest}: {exc}") from exc
