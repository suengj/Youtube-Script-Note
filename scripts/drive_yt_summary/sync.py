#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Core sync: local Obsidian summaries → Drive Desktop YT_summary/source + manifest."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from filename_utils import MAX_FILENAME_BYTES, parse_note_name

from .config import DriveSyncConfigError, load_config, verify_sync_root
from .fs_transport import FilesystemSyncError, atomic_write_text, copy_or_update_file, ensure_dir
from .legacy import migrate_contents_gen_to_legacy
from .manifest import (
    build_manifest_yaml,
    parse_manifest_yaml,
    restore_missing_manifest_fields,
    serialize_manifest_yaml,
    utc_now_iso,
)
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


def _verify_source_write(path: Path, expected_hash: str) -> None:
    try:
        actual_hash = _file_hash(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise FilesystemSyncError(f"source readback verification failed for {path}: {exc}") from exc
    if actual_hash != expected_hash:
        raise FilesystemSyncError(f"source readback verification failed for {path}: content hash mismatch")


def _manifest_item_identity(item: dict) -> tuple[str, str]:
    for key in ("drive_file_id", "video_id", "file"):
        value = str(item.get(key) or "").strip()
        if value:
            return key, value
    return "", ""


def _manifest_item_identities(doc: dict) -> set[tuple[str, str]]:
    return {_manifest_item_identity(item) for item in doc.get("items", []) if _manifest_item_identity(item)[1]}


def _merge_manifest_doc(existing: dict, generated: dict) -> dict:
    """Merge desktop sync entries without dropping canonical or unknown fields."""
    result = dict(existing)
    items = [dict(item) for item in existing.get("items", [])]

    for source in generated.get("items", []):
        matches = [i for i, item in enumerate(items) if (
            (source.get("drive_file_id") and item.get("drive_file_id") == source.get("drive_file_id")) or
            (source.get("video_id") and item.get("video_id") == source.get("video_id")) or
            (item.get("file") == source.get("file"))
        )]
        if len(matches) > 1:
            raise ValueError(f"manifest has multiple entries for desktop source {source.get('file')}")
        if not matches:
            items.append(dict(source))
            continue

        index = matches[0]
        old = dict(items[index])
        old_video_id = str(old.get("video_id") or "").strip()
        new_video_id = str(source.get("video_id") or "").strip()
        if old_video_id and new_video_id and old_video_id != new_video_id:
            raise ValueError(f"manifest video identity conflicts for {source.get('file')}")
        old_hash = str(old.get("hash") or "")
        new_hash = str(source.get("hash") or "")
        merged = dict(old)
        for key in ("file", "title", "video_id", "hash", "date"):
            if key in source:
                merged[key] = source[key]
        if old_hash and new_hash and old_hash != new_hash:
            try:
                merged["revision"] = int(old.get("revision", 1)) + 1
            except (TypeError, ValueError) as exc:
                raise ValueError(f"manifest revision is invalid for {source.get('file')}") from exc
            merged["content_revision_at"] = utc_now_iso()
        elif "revision" in old:
            merged["revision"] = old["revision"]
        elif "revision" in source:
            merged["revision"] = source["revision"]
        items[index] = merged

    result["items"] = items
    result.setdefault("version", generated.get("version", 1))
    result.setdefault("generated_at", generated.get("generated_at", utc_now_iso()))
    return result


def _read_manifest(path: Path) -> tuple[bytes, dict]:
    if not path.exists():
        return b"", {"version": 1, "items": []}
    payload = path.read_bytes()
    return payload, parse_manifest_yaml(payload)


def _write_merged_manifest(path: Path, new_files, titles_by_rel, *, max_attempts: int = 3) -> None:
    """Merge and verify the mounted manifest, retrying detected local clobbers.

    Drive Desktop exposes local bytes rather than the Drive API's headRevisionId.
    The local SHA-256/MD5 pre-write check and post-write identity check detect
    same-host races; the API sink remains the only production writer.
    """
    generated = parse_manifest_yaml(build_manifest_yaml(new_files, titles_by_rel))
    preserved_docs = []
    for _attempt in range(max_attempts):
        before_bytes, before_doc = _read_manifest(path)
        preserved_docs.append(before_doc)
        signature = (hashlib.sha256(before_bytes).hexdigest(), hashlib.md5(before_bytes).hexdigest())
        try:
            restored_doc, _restored = restore_missing_manifest_fields(before_doc, preserved_docs)
        except ValueError as exc:
            raise FilesystemSyncError(f"cannot safely restore manifest fields: {exc}") from exc
        merged = _merge_manifest_doc(restored_doc, generated)
        expected = serialize_manifest_yaml(merged)
        latest_bytes, latest_doc = _read_manifest(path)
        preserved_docs.append(latest_doc)
        latest_signature = (hashlib.sha256(latest_bytes).hexdigest(), hashlib.md5(latest_bytes).hexdigest())
        if latest_signature != signature:
            continue
        try:
            atomic_write_text(path, expected.decode("utf-8"))
            readback = path.read_bytes()
            readback_doc = parse_manifest_yaml(readback)
            preserved_docs.append(readback_doc)
        except (OSError, FilesystemSyncError, ValueError) as exc:
            continue
        previous_ids = _manifest_item_identities(before_doc)
        readback_ids = _manifest_item_identities(readback_doc)
        if (hashlib.sha256(readback).digest() == hashlib.sha256(expected).digest() and
                previous_ids.issubset(readback_ids)):
            return
        # A different but valid readback may be a concurrent write. Preserve both
        # observed documents and rebuild from the newest bytes on the next pass.
    raise FilesystemSyncError("manifest changed during merge/retries; refusing a lossy write")


def _manifest_write_authority(manifest_path: Path) -> Optional[str]:
    backend = os.getenv("P03_DRIVE_BACKEND", "api" if sys.platform.startswith("linux") else "desktop").strip().lower()
    if backend == "api":
        return "P03_DRIVE_BACKEND=api selects DriveApiSink as the production manifest writer"
    if backend != "desktop":
        return "P03_DRIVE_BACKEND must be desktop to authorize a Drive Desktop manifest write"
    explicit_desktop = os.getenv("P03_MANIFEST_WRITER", "").strip().lower() == "desktop"
    try:
        _payload, doc = _read_manifest(manifest_path)
    except Exception as exc:
        return f"existing manifest cannot be safely merged: {type(exc).__name__}: {exc}"
    lifecycle_fields = {"finalized_at", "content_revision_at", "recovered_at"}
    has_producer_fields = (
        "inventory" in doc or any(lifecycle_fields.intersection(item) for item in doc.get("items", []))
    )
    if has_producer_fields and not explicit_desktop:
        return "manifest contains inventory/lifecycle fields; explicit P03_MANIFEST_WRITER=desktop is required for a preserving merge"
    if not explicit_desktop:
        return "set P03_MANIFEST_WRITER=desktop to explicitly authorize the Drive Desktop manifest writer"
    # Explicit desktop authority permits a merge, but never a rebuild. The merge
    # helper below preserves these fields and unrelated inventory data.
    return None


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
      (f) the canonical parses as a complete P03 name (``filename_utils.parse_note_name``)
          and the candidate does NOT (a note with a complete ID + lang/subs + suffix is
          well-formed and never skipped; only a cut ID or suffix qualifies);
      (g) exactly one canonical matches.
    """
    folder, sep, name = rel.rpartition("/")
    if not sep or not name.endswith(".md"):
        return None
    cand = _nfd_bytes(name)
    if not (_TRUNC_MIN_BYTES <= len(cand) <= _TRUNC_MAX_BYTES):
        return None
    stem = cand[:-3]
    # A candidate with a complete, parseable ID + lang/subs + suffix is a well-formed
    # note (fit_filename always keeps them): never a truncation, whatever else matches.
    if parse_note_name(name) is not None:
        return None
    matches = []
    for other, entry in state_files.items():
        o_folder, o_sep, o_name = other.rpartition("/")
        if not o_sep or o_folder != folder or not o_name.endswith(".md"):
            continue
        canon = _nfd_bytes(o_name)
        # (c) strict prefix. Because the candidate's bytes equal the canonical's up to
        # the cut, any ID-shaped segment or lang/subs marker the candidate still holds is
        # the canonical's own; a different ID anywhere breaks the prefix.
        if not (len(canon) > _TRUNC_MAX_BYTES and len(canon[:-3]) > len(stem) and canon.startswith(stem)):
            continue
        # The canonical must be a complete P03 name, otherwise nothing proves a cut.
        if parse_note_name(o_name) is None:
            continue
        if _same_content(entry, content_hash):
            matches.append(other)
    # Ambiguous (e.g. ID cut before it: two videos share the title prefix): don't skip.
    return matches[0] if len(matches) == 1 else None


def _classify(dest: Path, sync_root: Path, deadline: Optional[float] = None) -> None:
    """SUE-1327: classify-if-needed after a written Drive copy (flag-gated, never raises)."""
    if deadline is not None and time.time() >= deadline:
        return
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
    classification_deadline: Optional[float] = None,
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
    manifest_path = config.sync_root / "manifest.yaml"
    if not dry_run:
        authority_error = _manifest_write_authority(manifest_path)
        if authority_error:
            result.errors += 1
            result.manifest_path = str(manifest_path)
            result.error_messages.append(authority_error)
            return result

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
        try:
            _old_payload, existing_doc = _read_manifest(manifest_path)
            generated_doc = parse_manifest_yaml(build_manifest_yaml(state.files, titles_by_rel))
            manifest_yaml = serialize_manifest_yaml(_merge_manifest_doc(existing_doc, generated_doc)).decode("utf-8")
        except Exception as exc:
            result.errors += 1
            result.error_messages.append(f"manifest preview merge failed: {exc}")
            return result
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
    # Cached state is useful for persistence and skip decisions, but only a
    # source file written successfully in this run is authority to change its
    # canonical manifest item.
    written_files: Dict[str, SyncStateEntry] = {}
    for rel, item in sorted(local_map.items()):
        dest = config.source_dir / item.drive_name
        prev = state.files.get(rel)
        try:
            if prev is None and find_canonical_for_truncated(rel, item.content_hash, state.files):
                result.skipped += 1
                result.actions.append(f"skipped: {rel} (truncated copy of an existing canonical note)")
            elif prev is None:
                copy_or_update_file(Path(item.absolute_path), dest, item.content)
                _verify_source_write(dest, item.content_hash)
                new_files[rel] = SyncStateEntry(
                    relative_path=rel,
                    content_hash=item.content_hash,
                    dest_path=str(dest),
                    drive_name=item.drive_name,
                    updated_at=_now_iso(),
                )
                written_files[rel] = new_files[rel]
                result.created += 1
                result.actions.append(f"created: {rel} → {dest}")
                _classify(dest, config.sync_root, classification_deadline)
            elif prev.content_hash == item.content_hash:
                result.skipped += 1
            else:
                copy_or_update_file(Path(item.absolute_path), Path(prev.dest_path), item.content)
                _verify_source_write(Path(prev.dest_path), item.content_hash)
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
                written_files[rel] = new_files[rel]
                result.updated += 1
                result.actions.append(f"updated: {rel} → {prev.dest_path}")
                _classify(Path(prev.dest_path), config.sync_root, classification_deadline)
        except FilesystemSyncError as exc:
            result.errors += 1
            result.error_messages.append(f"{rel}: {exc}")

    for rel in state.files.keys():
        if rel not in local_map:
            result.skipped += 1

    try:
        _write_merged_manifest(manifest_path, written_files, titles_by_rel)
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
    classification_deadline: Optional[float] = None,
) -> SyncResult:
    """Entry for main.py — never raises; failures returned in SyncResult."""
    try:
        return run_sync(
            dry_run=dry_run,
            migrate_legacy=migrate_legacy,
            limit=limit,
            date_folder=date_folder,
            backfill_date=backfill_date,
            classification_deadline=classification_deadline,
        )
    except Exception as exc:
        result = SyncResult(dry_run=dry_run, errors=1)
        result.error_messages.append(str(exc))
        return result
