#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Migrate contents_gen under YT_summary/legacy/ via local filesystem."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .fs_transport import FilesystemSyncError, ensure_dir, move_dir


@dataclass(frozen=True)
class LegacyMigrationResult:
    legacy_path: str
    contents_gen_path: str
    moved: bool
    message: str


def migrate_contents_gen_to_legacy(sync_root: Path, *, dry_run: bool = False) -> LegacyMigrationResult:
    legacy_dir = sync_root / "legacy"
    contents_at_root = sync_root / "contents_gen"
    contents_under_legacy = legacy_dir / "contents_gen"

    if contents_under_legacy.is_dir():
        return LegacyMigrationResult(
            legacy_path=str(legacy_dir),
            contents_gen_path=str(contents_under_legacy),
            moved=False,
            message="contents_gen already under legacy/",
        )

    if not contents_at_root.is_dir():
        return LegacyMigrationResult(
            legacy_path=str(legacy_dir),
            contents_gen_path="",
            moved=False,
            message="contents_gen not found at YT_summary root; no migration performed",
        )

    if dry_run:
        return LegacyMigrationResult(
            legacy_path=str(legacy_dir),
            contents_gen_path=str(contents_under_legacy),
            moved=True,
            message="Would move contents_gen → legacy/contents_gen",
        )

    ensure_dir(legacy_dir)
    try:
        move_dir(contents_at_root, contents_under_legacy)
    except FilesystemSyncError as exc:
        # Fallback: copytree if cross-filesystem move fails
        if contents_under_legacy.exists():
            return LegacyMigrationResult(
                legacy_path=str(legacy_dir),
                contents_gen_path=str(contents_under_legacy),
                moved=False,
                message=f"legacy migration failed: {exc}",
            )
        shutil.copytree(contents_at_root, contents_under_legacy)
        shutil.rmtree(contents_at_root)
    return LegacyMigrationResult(
        legacy_path=str(legacy_dir),
        contents_gen_path=str(contents_under_legacy),
        moved=True,
        message="Moved contents_gen → legacy/contents_gen",
    )
