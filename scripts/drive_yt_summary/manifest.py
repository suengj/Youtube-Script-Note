#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build YT_summary/manifest.yaml inventory."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Dict, List, Optional

import yaml

from scripts.note_catalog_utils import DATE_FOLDER_PATTERN, normalize_date

from .state import SyncStateEntry


def utc_now_iso() -> str:
    """Return an actual UTC timestamp suitable for lifecycle fields."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_manifest_yaml(payload: bytes | str) -> dict:
    """Parse and validate a canonical manifest document without discarding fields."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig")
    doc = yaml.safe_load(payload)
    if not isinstance(doc, dict):
        raise ValueError("manifest must be a YAML mapping")
    items = doc.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("manifest items must be a list of mappings")
    return doc


def serialize_manifest_yaml(doc: dict) -> bytes:
    if not isinstance(doc, dict):
        raise ValueError("manifest must be a mapping")
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode("utf-8")


def restore_missing_manifest_fields(doc: dict, prior_docs) -> tuple[dict, bool]:
    """Restore previously observed manifest fields lost to a concurrent write.

    Existing values win. A prior item is matched by any stable manifest
    identity; ambiguous matches fail closed rather than attaching fields to the
    wrong item.
    """
    restored = copy.deepcopy(doc)
    restored_items = restored.get("items", [])
    changed = False

    def merge_missing(current, prior):
        merged_any = False
        for key, value in prior.items():
            if key not in current:
                current[key] = copy.deepcopy(value)
                merged_any = True
            elif isinstance(current[key], dict) and isinstance(value, dict):
                merged_any = merge_missing(current[key], value) or merged_any
        return merged_any

    def tokens(item):
        return {
            (key, str(item.get(key) or "").strip())
            for key in ("drive_file_id", "video_id", "file")
            if str(item.get(key) or "").strip()
        }

    for prior in prior_docs:
        for key, value in prior.items():
            if key == "items":
                continue
            if key not in restored:
                restored[key] = copy.deepcopy(value)
                changed = True
            elif isinstance(restored[key], dict) and isinstance(value, dict):
                changed = merge_missing(restored[key], value) or changed

        for item in prior.get("items", []):
            item_tokens = tokens(item)
            matches = [current for current in restored_items
                       if item_tokens and item_tokens & tokens(current)]
            if not matches and item_tokens:
                restored_items.append(copy.deepcopy(item))
                changed = True
            elif len(matches) == 1:
                changed = merge_missing(matches[0], item) or changed
            elif len(matches) > 1:
                raise ValueError("cannot safely restore fields for an ambiguous manifest item")
    return restored, changed


def merge_manifest_source(
    doc: dict,
    *,
    file: str,
    title: str,
    content_hash: str,
    video_id: str,
    drive_file_id: str = "",
    recovered: bool = False,
    now: Optional[str] = None,
) -> tuple[dict, dict, bool]:
    """Merge one finalized summary while preserving every unrelated manifest field.

    Returns ``(document, change, changed)``. A same-key, same-hash source is a
    strict no-op, including for legacy entries without lifecycle timestamps.
    """
    if not isinstance(doc, dict):
        raise ValueError("manifest must be a mapping")
    if not isinstance(doc.get("items"), list) or any(not isinstance(x, dict) for x in doc["items"]):
        raise ValueError("manifest items must be a list of mappings")
    file = str(file or "").strip()
    title = str(title or file).strip()
    content_hash = str(content_hash or "").strip().lower()
    video_id = str(video_id or "").strip()
    drive_file_id = str(drive_file_id or "").strip()
    if not file or not content_hash or not video_id:
        raise ValueError("manifest source requires file, SHA-256 hash, and video_id")
    if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash):
        raise ValueError("manifest source hash must be a SHA-256 hex digest")

    matches = []
    for index, item in enumerate(doc["items"]):
        by_drive_id = bool(drive_file_id and str(item.get("drive_file_id") or "") == drive_file_id)
        by_file = str(item.get("file") or "") == file
        by_video_id = str(item.get("video_id") or "") == video_id
        if by_drive_id or by_file or by_video_id:
            matches.append((index, item))
    if len(matches) > 1:
        raise ValueError(f"manifest has multiple entries for video {video_id}")

    stamp = now or utc_now_iso()
    updated = dict(doc)
    updated["items"] = list(doc["items"])
    source_key = drive_file_id or file
    if matches:
        index, old_item = matches[0]
        old_file = str(old_item.get("file") or "")
        old_video_id = str(old_item.get("video_id") or "")
        if old_video_id and old_video_id != video_id:
            raise ValueError(f"manifest video identity conflicts for {file}")
        if old_item.get("hash") == content_hash:
            if old_file == file:
                return doc, {"action": "unchanged", "source_id": source_key, "hash": content_hash}, False
            # A Drive file may retain its identity while its display name changes.
            # Keep the content revision/timestamps intact and carry the stable ID.
            item = dict(old_item)
            item.update({"file": file, "title": title})
            if drive_file_id:
                item["drive_file_id"] = drive_file_id
            updated["items"][index] = item
            return updated, {"action": "renamed", "source_id": source_key, "hash": content_hash}, True

        item = dict(old_item)
        if not item.get("hash"):
            # The previous bytes are unknown. Record the observed digest without
            # inventing a content revision or lifecycle timestamp.
            item["file"] = file
            item["title"] = title
            item["hash"] = content_hash
            item.setdefault("revision", 1)
            item.setdefault("video_id", video_id)
            if drive_file_id:
                item.setdefault("drive_file_id", drive_file_id)
            updated["items"][index] = item
            return updated, {"action": "recovered", "source_id": source_key, "hash": content_hash}, True

        try:
            revision = int(old_item.get("revision", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"manifest revision is invalid for {file}") from exc
        if revision < 0:
            raise ValueError(f"manifest revision is invalid for {file}")
        item.update({
            "file": file,
            "title": title,
            "hash": content_hash,
            "revision": revision + 1,
            "video_id": video_id,
        })
        if recovered:
            # The bytes were found during reconciliation, so the actual time this
            # content revision occurred is unknown. Record when we recovered it.
            item.pop("content_revision_at", None)
            item["recovered_at"] = stamp
        else:
            item["content_revision_at"] = stamp
        if drive_file_id:
            item["drive_file_id"] = drive_file_id
        updated["items"][index] = item
        return updated, {"action": "revised", "source_id": source_key, "hash": content_hash}, True

    item = {
        "file": file,
        "title": title,
        "hash": content_hash,
        "revision": 1,
        "video_id": video_id,
    }
    if drive_file_id:
        item["drive_file_id"] = drive_file_id
    if recovered:
        item["finalized_at"] = None
        item["recovered_at"] = stamp
    else:
        item["finalized_at"] = stamp
    updated["items"].append(item)
    return updated, {"action": "added", "source_id": source_key, "hash": content_hash}, True


def build_inventory_record(
    *,
    producer_run_id: str,
    coverage_started_at: str,
    cutoff,
    succeeded: int = 0,
    failed: int = 0,
    unresolved: int = 0,
    scope_count: Optional[int] = None,
    manifest_succeeded: Optional[int] = None,
    coverage_state_valid: bool = False,
    count_partition_verified: Optional[bool] = None,
    discovery_complete: bool = False,
    all_successes_present: bool = False,
    manifest_readback_verified: bool = False,
    completed: bool = False,
    inventory_revision: int = 0,
    now: Optional[str] = None,
) -> dict:
    """Construct the registry-facing producer inventory evidence block."""
    counts = {"succeeded": int(succeeded), "failed": int(failed), "unresolved": int(unresolved)}
    if any(value < 0 for value in counts.values()):
        raise ValueError("inventory source counts cannot be negative")
    counts["scope_count"] = int(scope_count if scope_count is not None else sum(counts.values()))
    counts["manifest_succeeded"] = int(manifest_succeeded if manifest_succeeded is not None else succeeded)
    counts["terminal_skipped"] = max(0, counts["succeeded"] - counts["manifest_succeeded"])
    partition_verified = (
        counts["succeeded"] + counts["failed"] + counts["unresolved"] == counts["scope_count"] and
        0 <= counts["manifest_succeeded"] <= counts["succeeded"]
    )
    if count_partition_verified is not None:
        partition_verified = partition_verified and bool(count_partition_verified)
    return {
        "inventory_revision": int(inventory_revision),
        "producer_run_id": str(producer_run_id),
        "coverage_started_at": str(coverage_started_at),
        "coverage_completed_at": (now or utc_now_iso()) if completed else None,
        "cutoff": cutoff,
        "source_counts": counts,
        "coverage_state_valid": bool(coverage_state_valid),
        "count_partition_verified": partition_verified,
        "discovery_complete": bool(discovery_complete),
        # These flags are only set in the detached completion evidence after the
        # manifest bytes and that evidence file have both passed readback.
        "manifest_readback_verified": False,
        "all_successes_present": bool(all_successes_present),
        "batch_completed": bool(completed),
        "inventory_complete": False,
    }


INVENTORY_SUMMARY_SUCCESS_STATUSES = {"success", "already_existed"}
INVENTORY_TERMINAL_SUCCESS_STATUSES = INVENTORY_SUMMARY_SUCCESS_STATUSES | {
    "passed_shorts", "short", "video_unavailable", "skipped_auto_subs_only", "oversized_file",
}
INVENTORY_TERMINAL_FAILURE_STATUSES = {
    "error", "download_failed", "mlx_error", "captions_error", "source_unavailable",
    "api_error", "file_error", "no_auto_caption", "manual_review", "durable_transcript_unreadable",
    "retry_exhausted",
}


def classify_inventory_status(status) -> str:
    """Map every status to succeeded, failed, or unresolved without a default PASS."""
    normalized = str(status or "").strip().casefold()
    if normalized in INVENTORY_TERMINAL_SUCCESS_STATUSES:
        return "succeeded"
    if normalized in INVENTORY_TERMINAL_FAILURE_STATUSES:
        return "failed"
    return "unresolved"


def inventory_completion_decision(
    inventory: dict,
    *,
    manifest_readback_verified: bool,
    inventory_file_readback_verified: bool,
    all_successes_present: bool,
) -> dict:
    """Central fail-closed decision for publishing ``inventory_complete``."""
    inventory = inventory if isinstance(inventory, dict) else {}
    counts = inventory.get("source_counts") if isinstance(inventory, dict) else None
    counts = counts if isinstance(counts, dict) else {}
    try:
        succeeded = int(counts.get("succeeded", -1))
        failed = int(counts.get("failed", -1))
        unresolved = int(counts.get("unresolved", -1))
        scope_count = int(counts.get("scope_count", -1))
        manifest_succeeded = int(counts.get("manifest_succeeded", succeeded))
    except (TypeError, ValueError):
        succeeded = failed = unresolved = scope_count = manifest_succeeded = -1
    partition_verified = (
        succeeded >= 0 and failed >= 0 and unresolved >= 0 and scope_count >= 0 and
        succeeded + failed + unresolved == scope_count and
        0 <= manifest_succeeded <= succeeded and
        inventory.get("count_partition_verified") is True
    )
    ready = bool(
        inventory.get("batch_completed") is True and
        inventory.get("discovery_complete") is True and
        inventory.get("coverage_state_valid") is True and
        partition_verified and failed == 0 and unresolved == 0 and
        all_successes_present is True and manifest_readback_verified is True
    )
    return {
        "ready_for_inventory_file_verification": ready,
        "inventory_complete": bool(ready and inventory_file_readback_verified is True),
    }


def set_manifest_inventory(doc: dict, inventory: dict, *, start: bool) -> tuple[dict, dict]:
    """Apply a batch inventory block with a monotonic per-run revision."""
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise ValueError("manifest document is malformed")
    if not isinstance(inventory, dict) or not str(inventory.get("producer_run_id") or "").strip():
        raise ValueError("inventory requires producer_run_id")
    current = doc.get("inventory")
    if current is not None and not isinstance(current, dict):
        raise ValueError("manifest inventory block is malformed")

    def current_revision() -> int:
        if not current:
            return 0
        try:
            revision = int(current["inventory_revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("manifest inventory revision is invalid") from exc
        if revision < 1:
            raise ValueError("manifest inventory revision is invalid")
        return revision

    # The producer owns known coverage fields, but newer or consumer-specific
    # metadata must survive a later producer run.
    record = {**(current or {}), **inventory}
    if start:
        if current and current.get("producer_run_id") == record.get("producer_run_id"):
            revision = current_revision()
        else:
            revision = current_revision() + 1
        record["inventory_revision"] = revision
    else:
        if not current or current.get("producer_run_id") != record.get("producer_run_id"):
            raise ValueError("inventory run was superseded before completion")
        record["inventory_revision"] = current_revision()
    updated = dict(doc)
    updated["inventory"] = record
    return updated, record


def reconcile_inventory_scope(scope_video_ids, outcomes, *, extra_unresolved: int = 0):
    """Classify every declared source exactly once into success/failure/unresolved."""
    scope = {str(value).strip() for value in scope_video_ids if str(value).strip()}
    normalized = {
        str(video_id).strip(): str(status or "").strip().lower()
        for video_id, status in (outcomes or {}).items()
        if str(video_id).strip()
    }
    scope.update(normalized)
    classifications = {video_id: classify_inventory_status(normalized.get(video_id, "")) for video_id in scope}
    success_ids = {video_id for video_id in scope
                   if normalized.get(video_id) in INVENTORY_SUMMARY_SUCCESS_STATUSES}
    succeeded_ids = {video_id for video_id, classification in classifications.items()
                     if classification == "succeeded"}
    failed_ids = {video_id for video_id, classification in classifications.items()
                  if classification == "failed"}
    unresolved = sum(classification == "unresolved" for classification in classifications.values())
    extra_unresolved = max(0, int(extra_unresolved or 0))
    unresolved += extra_unresolved
    succeeded = len(succeeded_ids)
    failed = len(failed_ids)
    scope_count = len(scope) + extra_unresolved
    partition_verified = succeeded + failed + unresolved == scope_count
    assert partition_verified, "inventory scope counts must classify every source exactly once"
    return {
        "succeeded": succeeded,
        "failed": failed,
        "unresolved": unresolved,
        "scope_count": scope_count,
        "manifest_succeeded": len(success_ids),
        "terminal_skipped": len(succeeded_ids - success_ids),
        "count_partition_verified": partition_verified,
        "success_video_ids": success_ids,
    }


def verify_inventory_evidence(
    manifest_bytes: bytes,
    evidence_bytes: bytes,
    *,
    evidence_md5: str = "",
    expected_manifest_file_id: str = "",
) -> dict:
    """Return validated inventory evidence only when both downloads are intact.

    Consumers must compare the evidence's manifest SHA-256 with the exact current
    canonical manifest bytes and, when Drive metadata is available, verify the
    evidence download against its metadata MD5.
    """
    import hashlib
    import json

    manifest_bytes = bytes(manifest_bytes)
    evidence_bytes = bytes(evidence_bytes)
    if evidence_md5 and hashlib.md5(evidence_bytes).hexdigest() != evidence_md5:
        raise ValueError("inventory evidence download does not match Drive metadata")
    evidence = json.loads(evidence_bytes.decode("utf-8"))
    inventory = evidence.get("inventory") if isinstance(evidence, dict) else None
    if not isinstance(inventory, dict) or inventory.get("inventory_complete") is not True:
        raise ValueError("inventory evidence is incomplete")
    decision = inventory_completion_decision(
        inventory,
        manifest_readback_verified=inventory.get("manifest_readback_verified") is True,
        inventory_file_readback_verified=inventory.get("inventory_file_readback_verified") is True,
        all_successes_present=inventory.get("all_successes_present") is True,
    )
    if not decision["inventory_complete"]:
        raise ValueError("inventory evidence does not prove complete verified coverage")
    if not inventory.get("coverage_completed_at") or int(inventory.get("inventory_revision", 0)) < 1:
        raise ValueError("inventory evidence has no verified coverage end or revision")
    if expected_manifest_file_id and evidence.get("manifest_file_id") != expected_manifest_file_id:
        raise ValueError("inventory evidence references a different canonical manifest ID")
    if evidence.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
        raise ValueError("inventory evidence does not match current manifest bytes")
    return evidence


def _date_from_relative_path(rel: str) -> Optional[str]:
    parts = rel.split("/")
    if len(parts) < 2:
        return None
    folder = parts[0]
    if not DATE_FOLDER_PATTERN.match(folder):
        return None
    y, mo, d = folder.split("_")
    return f"{y}-{mo}-{d}"


def build_manifest_yaml(
    entries: Dict[str, SyncStateEntry],
    titles_by_rel: Dict[str, str],
    dates_by_rel: Optional[Dict[str, str]] = None,
) -> str:
    items: List[dict] = []
    dates_by_rel = dates_by_rel or {}
    for rel in sorted(entries.keys()):
        entry = entries[rel]
        item: dict = {
            "file": entry.drive_name,
            "title": titles_by_rel.get(rel) or entry.title or entry.drive_name,
        }
        if entry.video_id:
            item["video_id"] = entry.video_id
        if entry.content_hash:
            item["hash"] = entry.content_hash
        item["revision"] = entry.revision
        date_val = dates_by_rel.get(rel) or _date_from_relative_path(rel)
        if date_val:
            normalized = normalize_date(date_val)
            if normalized:
                item["date"] = normalized
        items.append(item)
    doc = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "items": items,
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
