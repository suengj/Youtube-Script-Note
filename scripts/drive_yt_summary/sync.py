#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Core sync: local Obsidian summaries → Drive Desktop YT_summary/source + manifest."""

from __future__ import annotations

from dataclasses import dataclass, field
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from filename_utils import KNOWN_LLM_SUFFIXES, MAX_FILENAME_BYTES, parse_note_name

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


# Storage cuts an over-long NFD name to 255 bytes; a cut that lands mid-jamo can leave 253-254.
_TRUNC_MAX_BYTES = MAX_FILENAME_BYTES  # tied to the 255-byte HARD cap, not the 240 budget
_TRUNC_MIN_BYTES = MAX_FILENAME_BYTES - 2


def _nfd_bytes(text: str) -> bytes:
    return unicodedata.normalize("NFD", text).encode("utf-8")


def _same_content(entry: SyncStateEntry, content_hash: str) -> bool:
    recorded = (entry.content_hash or "").strip()
    if not recorded:
        try:
            recorded = _file_hash(Path(entry.dest_path))
        except (OSError, ValueError):
            return False
    return bool(recorded) and recorded == content_hash


def _file_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_text(encoding="utf-8-sig").encode("utf-8")).hexdigest()


def find_canonical_for_truncated(
    rel: str, content_hash: str, state_files: Dict[str, SyncStateEntry]
) -> Optional[str]:
    """Return the canonical state key when ``rel`` is a storage-truncated copy of it.

    iCloud/Obsidian cut over-long decomposed names to 255 bytes, so the vault can hold
    ``..._JyyAGZ1-r94_ko-orig_auto_subs_.md`` while the pipeline already published
    ``..._luna-low.md`` to Drive. A vault scan must not mint a second Drive file for it.
    Skip only when ALL hold:
      (a) the candidate's NFD name is 253..255 bytes;
      (b) the canonical's NFD name is > 255 bytes (it can only exist on the vault as a
          truncation);
      (c) the candidate's NFD stem (without ``.md``) is a strict byte prefix of the
          canonical's NFD stem;
      (d) the canonical is a sync-state entry in the same date folder. Entries whose
          folder cannot be established (e.g. loose ``source/`` files) never match;
      (e) the contents are equal: the canonical's recorded content hash equals
          ``content_hash`` or, when no hash is recorded, its Drive file hashes equal.
          Unknown hash or unreadable file means no skip;
      (f) the canonical parses as a complete P03 name (``filename_utils.parse_note_name``),
          the candidate is NOT itself well-formed (complete ID + full suffix), and a
          complete ID inside the candidate equals the canonical's ID;
      (g) exactly one canonical matches.
    """
    folder, sep, name = rel.rpartition("/")
    if not sep or not name.endswith(".md"):
        return None
    cand = _nfd_bytes(name)
    if not (_TRUNC_MIN_BYTES <= len(cand) <= _TRUNC_MAX_BYTES):
        return None
    stem = cand[:-3]
    cand_parts = parse_note_name(name)
    matches = []
    for other, entry in state_files.items():
        o_folder, o_sep, o_name = other.rpartition("/")
        if not o_sep or o_folder != folder or not o_name.endswith(".md"):
            continue
        canon = _nfd_bytes(o_name)
        if not (len(canon) > _TRUNC_MAX_BYTES and len(canon[:-3]) > len(stem) and canon.startswith(stem)):
            continue
        # (f) the canonical must be a complete P03 name, otherwise nothing proves a cut.
        canon_parts = parse_note_name(o_name)
        if canon_parts is None:
            continue
        # (g) a candidate that still carries a complete ID must carry the same ID, and a
        # well-formed candidate (ID + full suffix) is never a truncation. The one
        # exception is a suffix cut mid-word (``..._lu.md``), a strict prefix of the
        # canonical's suffix.
        marker = f"_{canon_parts.video_id}".encode("utf-8")
        at = canon.rfind(marker)
        if at >= 0 and len(stem) >= at + len(marker):
            if stem[at : at + len(marker)] != marker:
                continue
        if cand_parts is not None:
            if cand_parts.video_id != canon_parts.video_id:
                continue
            partial = (
                cand_parts.llm_suffix not in KNOWN_LLM_SUFFIXES
                and cand_parts.llm_suffix != canon_parts.llm_suffix
                and canon_parts.llm_suffix.startswith(cand_parts.llm_suffix)
            )
            if not partial:
                continue
        if _same_content(entry, content_hash):
            matches.append(other)
    # Ambiguous (e.g. ID cut before it: two videos share the title prefix): don't skip.
    return matches[0] if len(matches) == 1 else None


def _classify(dest: Path, sync_root: Path) -> None:
    """SUE-1327: classify-if-needed after a written Drive copy (flag-gated, never raises)."""
    try:
        from scripts.jev_classify.classify import classify_after_publish

        classify_after_publish(str(dest), sync_root)
    except Exception:
        pass


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
            if prev is None and find_canonical_for_truncated(rel, item.content_hash, state.files):
                result.skipped += 1
                result.actions.append(f"skip: {rel} (truncated copy of an existing canonical note)")
            elif prev is None:
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
            if prev is None and find_canonical_for_truncated(rel, item.content_hash, state.files):
                result.skipped += 1
                result.actions.append(f"skipped: {rel} (truncated copy of an existing canonical note)")
            elif prev is None:
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
                _classify(dest, config.sync_root)
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
                    video_id=prev.video_id,
                    title=prev.title or item.title,
                    revision=prev.revision + 1,
                )
                result.updated += 1
                result.actions.append(f"updated: {rel} → {prev.dest_path}")
                _classify(Path(prev.dest_path), config.sync_root)
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
