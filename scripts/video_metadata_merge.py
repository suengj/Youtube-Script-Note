#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Merge video_metadata_live.jsonl + video_metadata_offline.jsonl → video_metadata_merged.jsonl.

md_path 기준 dedupe: live 우선 (동일 md_path 있으면 live 기록 유지).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

try:
    from config import resolve_data_root
except ImportError:
    def resolve_data_root(bp, wp=None):
        return os.path.abspath(bp)

LIVE_JSONL = "video_metadata_live.jsonl"
OFFLINE_JSONL = "video_metadata_offline.jsonl"
MERGED_JSONL = "video_metadata_merged.jsonl"
LEGACY_JSONL = "video_metadata.jsonl"  # legacy: migrate to live if live empty


def load_jsonl(path: str) -> list[dict]:
    """Load JSONL file. Returns list of records."""
    if not os.path.isfile(path):
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _ensure_has_yid(rec: dict) -> dict:
    """Ensure has_yid exists (backward compat for legacy JSONL)."""
    if "has_yid" not in rec:
        rec["has_yid"] = bool(rec.get("v_id", ""))
    return rec


def merge(live_records: list[dict], offline_records: list[dict]) -> list[dict]:
    """
    Merge live + offline. Dedupe by md_path (normalized).
    Prefer live over offline when both have same md_path.
    """
    by_path: dict[str, dict] = {}
    # Offline first (lower priority)
    for rec in offline_records:
        p = rec.get("md_path", "")
        if p:
            by_path[os.path.normpath(p)] = _ensure_has_yid(dict(rec))
    # Live overwrites (higher priority)
    for rec in live_records:
        p = rec.get("md_path", "")
        if p:
            by_path[os.path.normpath(p)] = _ensure_has_yid(dict(rec))
    return list(by_path.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge live + offline JSONL")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true", help="계획만 출력")
    args = parser.parse_args()

    base = args.base_path
    if not base or not os.path.isdir(base):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
        base = os.getenv("BASE_PATH", "").strip()
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.abspath(base)
    work = os.getenv("WORK_PATH", "").strip() or None
    data_root = resolve_data_root(base, work)

    def _pick_jsonl(name: str) -> str:
        p_dr = os.path.join(data_root, name)
        p_base = os.path.join(base, name)
        if os.path.isfile(p_dr):
            return p_dr
        if os.path.isfile(p_base):
            return p_base
        return p_dr

    live_path = _pick_jsonl(LIVE_JSONL)
    offline_path = _pick_jsonl(OFFLINE_JSONL)
    merged_path = os.path.join(data_root, MERGED_JSONL)
    legacy_path = os.path.join(base, LEGACY_JSONL)
    if not os.path.isfile(legacy_path):
        leg_dr = os.path.join(data_root, LEGACY_JSONL)
        if os.path.isfile(leg_dr):
            legacy_path = leg_dr

    live = load_jsonl(live_path)
    if not live and os.path.isfile(legacy_path):
        live = load_jsonl(legacy_path)
        for rec in live:
            rec.setdefault("has_yid", True)
        print(f"(Legacy: {len(live)} from {LEGACY_JSONL})")
    offline = load_jsonl(offline_path)
    merged = merge(live, offline)

    print("=" * 60)
    print("video_metadata Merge")
    print("=" * 60)
    print(f"Live:   {len(live)} records ({live_path})")
    print(f"Offline: {len(offline)} records ({offline_path})")
    print(f"Merged: {len(merged)} unique (by md_path)")
    print(f"Output: {merged_path}")

    if args.dry_run:
        print("\n[DRY-RUN] No write.")
        return

    with open(merged_path, "w", encoding="utf-8") as f:
        for rec in merged:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\nWrote {merged_path}")


if __name__ == "__main__":
    main()
