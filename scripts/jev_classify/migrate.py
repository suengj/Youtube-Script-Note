"""One-time cutover of the intelligence-library v1 index (src-gdrive-* keys) to P03 v2 (video_id keys).

No provider calls. Result fields are copied verbatim; v1-only inventory fields (producer,
id_source, front_matter_mode, inventory_signature, source_id, drive_file_id) move to `lineage`
or are dropped. Entries for the same video_id with identical content_hash are merged (newest
classified_at wins; the others' Drive IDs/source IDs are kept in lineage, and the full v1
bytes stay in classification-index.json.prev). Written in place (same inode) with a .prev backup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .classify import video_id_from_url
from .index import OWNER, SCHEMA_VERSION, cache_key, update_index

PRESERVED = ("content_hash", "category_scores", "reason", "score_semantics", "taxonomy_version", "classifier",
             "model", "prompt_version", "classified_at", "stale", "error", "cache_key", "details")


def convert(v1: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if v1.get("schema_version") != 1 or not isinstance(v1.get("entries"), dict):
        raise ValueError("input is not a schema_version 1 classification index")
    groups: dict[str, list[tuple[str, dict]]] = {}
    unresolved: list[str] = []
    for source_id, entry in v1["entries"].items():
        vid = video_id_from_url(entry.get("source_url", "")) if isinstance(entry, dict) else None
        if not vid:
            unresolved.append(source_id)
            continue
        groups.setdefault(vid, []).append((source_id, entry))

    entries: dict[str, Any] = {}
    conflicts: list[str] = []
    merged = 0
    for vid, items in groups.items():
        items.sort(key=lambda kv: (kv[1].get("classified_at") or "", kv[0]), reverse=True)
        if len({e.get("content_hash") for _, e in items}) > 1:
            conflicts.append(vid)  # different bytes for one video: keep newest, the rest retries on next publish
        _winner_id, winner = items[0]
        merged += len(items) - 1
        new = {k: winner[k] for k in PRESERVED if k in winner}
        new["video_id"] = vid
        new["source_url"] = f"https://www.youtube.com/watch?v={vid}"
        new["lineage"] = {
            "drive_file_ids": [e["drive_file_id"] for _, e in items if e.get("drive_file_id")],
            "legacy_source_ids": [sid for sid, _ in items],
            "migrated_from": "intelligence-library schema_version 1",
        }
        entries[vid] = new

    v2 = {k: v1[k] for k in ("taxonomy_version", "classifier_config") if k in v1}
    v2.update({"schema_version": SCHEMA_VERSION, "owner": OWNER, "entries": entries})
    report = {"before": len(v1["entries"]), "after": len(entries), "duplicates_merged": merged,
              "duplicate_video_ids": sorted(vid for vid, items in groups.items() if len(items) > 1),
              "hash_conflicts": sorted(conflicts), "unresolvable": sorted(unresolved)}
    report.update(parity(v1, v2))
    return v2, report


def parity(v1: dict[str, Any], v2: dict[str, Any]) -> dict[str, Any]:
    """Every v1 entry maps to a v2 entry; winners match field-for-field; cache keys recompute."""
    mismatched: list[str] = []
    cache_key_mismatch = 0
    winners = 0
    for source_id, old in v1["entries"].items():
        vid = video_id_from_url(old.get("source_url", ""))
        new = v2["entries"].get(vid) if vid else None
        if new is None:
            mismatched.append(source_id)
            continue
        if source_id not in new["lineage"]["legacy_source_ids"]:
            mismatched.append(source_id)
            continue
        if new["lineage"]["legacy_source_ids"][0] != source_id:
            if new.get("content_hash") != old.get("content_hash"):
                mismatched.append(source_id)
            continue
        winners += 1
        if any(old.get(k) != new.get(k) for k in PRESERVED):
            mismatched.append(source_id)
        recomputed = cache_key(new["content_hash"], new["taxonomy_version"], new["classifier"], new["model"],
                               new["prompt_version"])
        if not new.get("error") and recomputed != new.get("cache_key"):
            cache_key_mismatch += 1
    return {"parity_winner_entries": winners, "parity_mismatches": mismatched,
            "cache_key_mismatches": cache_key_mismatch}


def migrate_file(index_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    from .index import validate

    report: dict[str, Any] = {}

    def mutate(index: dict) -> bool:
        if index.get("schema_version") == SCHEMA_VERSION:
            report["already_migrated"] = True
            return False
        v2, rep = convert(index)
        validate(v2)
        report.update(rep)
        if rep["parity_mismatches"] or rep["cache_key_mismatches"]:
            raise ValueError("migration parity failed; index left unchanged")
        if dry_run:
            return False
        index.clear()
        index.update(v2)
        return True

    report["written"] = update_index(index_path, mutate, allow_schema_v1=True)
    if report["written"]:
        after = json.loads(index_path.read_text(encoding="utf-8"))
        validate(after)
        report["readback_entries"] = len(after["entries"])
    return report
