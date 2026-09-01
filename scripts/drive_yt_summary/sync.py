#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Core sync: local Obsidian summaries → Drive Desktop YT_summary/source + manifest."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import DriveSyncConfigError, load_config, verify_sync_root
from .fs_transport import FilesystemSyncError, atomic_write_text, copy_or_update_file, ensure_dir
from .legacy import migrate_contents_gen_to_legacy
from .manifest import build_manifest_yaml
from .scanner import scan_summary_map
from .state import SyncState, SyncStateEntry, load_state, save_state


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    duplicate: int = 0
    errors: int = 0
    dry_run: bool = False
    legacy_message: str = ""
    sync_root: str = ""
    source_dir: str = ""
    manifest_path: str = ""
    actions: List[str] = field(default_factory=list)
    error_messages: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "duplicate": self.duplicate,
            "errors": self.errors,
            "dry_run": self.dry_run,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_sync(
    *,
    dry_run: bool = False,
    migrate_legacy: bool = True,
    limit: Optional[int] = None,
    date_folder: Optional[str] = None,
    backfill_date: Optional[str] = None,
    base_path: Optional[str] = None,
    work_path: Optional[str] = None,
    md_path: Optional[str] = None,
    sync_root: Optional[str] = None,
) -> SyncResult:
    result = SyncResult(dry_run=dry_run)
    bounded_date = backfill_date or date_folder

    try:
        config = load_config(base_path, work_path, md_path, sync_root=sync_root)
    except DriveSyncConfigError as exc:
        result.errors += 1
        result.error_messages.append(str(exc))
        return result

    if not config.enabled:
        result.legacy_message = "Drive sync disabled (P03_DRIVE_SYNC_ENABLED=0)"
        result.actions.append("skip: sync disabled")
        return result

    result.sync_root = str(config.sync_root)
    result.source_dir = str(config.source_dir)

    local_map = scan_summary_map(config.md_root, limit=limit, date_folder=bounded_date)
    titles_by_rel = {rel: item.title for rel, item in local_map.items()}

    state = load_state(config.state_path)

    if dry_run:
        if migrate_legacy:
            legacy_plan = migrate_contents_gen_to_legacy(config.sync_root, dry_run=True)
            result.legacy_message = legacy_plan.message
        for rel, item in sorted(local_map.items()):
            dest = config.source_dir / item.drive_name
            prev = state.files.get(rel)
            if prev is None:
                result.created += 1
                result.actions.append(f"create: {rel} → {dest}")
            elif prev.content_hash == item.content_hash:
                result.skipped += 1
                result.actions.append(f"skip: {rel} (unchanged)")
            else:
                result.updated += 1
                result.actions.append(f"update: {rel} → {dest}")
        manifest_yaml = build_manifest_yaml(state.files, titles_by_rel)
        result.actions.append(
            f"manifest: {len(state.files)} active items → {config.sync_root / 'manifest.yaml'} "
            f"({len(manifest_yaml)} bytes)"
        )
        return result

    try:
        verify_sync_root(config.sync_root)
        ensure_dir(config.source_dir)
    except (DriveSyncConfigError, FilesystemSyncError) as exc:
        result.errors += 1
        result.error_messages.append(str(exc))
        return result

    if migrate_legacy:
        legacy_result = migrate_contents_gen_to_legacy(config.sync_root, dry_run=False)
        result.legacy_message = legacy_result.message

    new_files: Dict[str, SyncStateEntry] = dict(state.files)
    manifest_path = config.sync_root / "manifest.yaml"

    for rel, item in sorted(local_map.items()):
        dest = config.source_dir / item.drive_name
        prev = state.files.get(rel)
        try:
            if prev is None:
                copy_or_update_file(Path(item.absolute_path), dest, item.content)
                new_files[rel] = SyncStateEntry(
                    relative_path=rel,
                    content_hash=item.content_hash,
                    dest_path=str(dest),
                    drive_name=item.drive_name,
                    updated_at=_now_iso(),
                )
                result.created += 1
                result.actions.append(f"created: {rel} → {dest}")
            elif prev.content_hash == item.content_hash:
                result.skipped += 1
            else:
                copy_or_update_file(Path(item.absolute_path), Path(prev.dest_path), item.content)
                new_files[rel] = SyncStateEntry(
                    relative_path=rel,
                    content_hash=item.content_hash,
                    dest_path=prev.dest_path,
                    drive_name=item.drive_name,
                    updated_at=_now_iso(),
                )
                result.updated += 1
                result.actions.append(f"updated: {rel} → {prev.dest_path}")
        except FilesystemSyncError as exc:
            result.errors += 1
            result.error_messages.append(f"{rel}: {exc}")

    for rel in state.files.keys():
        if rel not in local_map:
            result.skipped += 1

    manifest_yaml = build_manifest_yaml(new_files, titles_by_rel)
    try:
        atomic_write_text(manifest_path, manifest_yaml)
        result.manifest_path = str(manifest_path)
    except FilesystemSyncError as exc:
        result.errors += 1
        result.error_messages.append(f"manifest: {exc}")

    if result.errors == 0:
        save_state(
            config.state_path,
            SyncState(files=new_files, manifest_path=str(manifest_path)),
        )

    return result


def run_sync_safe(
    *,
    dry_run: bool = False,
    migrate_legacy: bool = True,
    limit: Optional[int] = None,
    date_folder: Optional[str] = None,
    backfill_date: Optional[str] = None,
) -> SyncResult:
    """Entry for main.py — never raises; failures returned in SyncResult."""
    try:
        return run_sync(
            dry_run=dry_run,
            migrate_legacy=migrate_legacy,
            limit=limit,
            date_folder=date_folder,
            backfill_date=backfill_date,
        )
    except Exception as exc:
        result = SyncResult(dry_run=dry_run, errors=1)
        result.error_messages.append(str(exc))
        return result
