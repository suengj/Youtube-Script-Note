#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 1: 과거 영상 MD → video_metadata_offline.jsonl 구축.

YID가 있는 Obsidian MD 파일을 스캔하여 YouTube Data API로 upload_date를
50개 단위 배치 조회 후 video_metadata_offline.jsonl에 append.
YOUTUBE_API_KEY 필요 (.env). CPU/네트워크 부하 최소화.

Usage:
  python scripts/md_to_offline_jsonl_phase1.py [--base-path DIR] [--md-path DIR] [--dry-run]
  --dry-run  계획만 출력, JSONL 미기록
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

from vid_to_title_rename import (
    extract_vid_from_md_name,
    extract_vid_from_md_in_subdir,
    get_paths,
)
from youtube_api_metadata import fetch_video_metadata_batch, get_api_key

try:
    from config import resolve_data_root
except ImportError:
    def resolve_data_root(bp, wp=None):
        return os.path.abspath(bp)

# Reuse yid_precheck for scan_md_files only
import importlib.util
_yid_spec = importlib.util.spec_from_file_location(
    "yid_precheck",
    os.path.join(os.path.dirname(__file__), "yid_precheck.py"),
)
yid_precheck = importlib.util.module_from_spec(_yid_spec)
_yid_spec.loader.exec_module(yid_precheck)

VID_IN_WHISPER = re.compile(r"\+vid-([A-Za-z0-9_-]{11})")
OFFLINE_JSONL = "video_metadata_offline.jsonl"
BATCH_DELAY = 0.5  # API는 quota 기반, 짧은 딜레이만

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


def extract_vid_from_md(rel_path: str, filename: str, in_subdir: bool) -> str | None:
    """Extract v_id from MD path. Handles flat, subdir, +vid-VID."""
    date_prefix, vid, _ = extract_vid_from_md_name(filename)
    if vid:
        return vid
    vid, _ = extract_vid_from_md_in_subdir(filename) if in_subdir else (None, None)
    if vid:
        return vid
    m = VID_IN_WHISPER.search(filename)
    if m:
        return m.group(1)
    return None


def load_existing_md_paths(jsonl_path: str) -> set[str]:
    """Load md_path values from existing JSONL to skip duplicates."""
    seen = set()
    if not os.path.isfile(jsonl_path):
        return seen
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                p = rec.get("md_path", "")
                if p:
                    seen.add(os.path.normpath(p))
    except Exception:
        pass
    return seen


def append_offline_record(jsonl_path: str, record: dict) -> None:
    """Append one record to video_metadata_offline.jsonl."""
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Phase 1: 과거 영상 MD → offline JSONL (YouTube API)")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--md-path", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true", help="계획만 출력")
    args = parser.parse_args()

    api_key = get_api_key()
    if not api_key:
        print("YOUTUBE_API_KEY required in .env", file=sys.stderr)
        sys.exit(1)

    base, _, _ = get_paths(args.base_path)
    base = os.path.abspath(base)
    data_root = resolve_data_root(base, os.getenv("WORK_PATH", "").strip() or None)
    md_path = args.md_path or os.getenv("OUTPUT_MD_PATH", "").strip()
    if not md_path:
        md_path = get_paths(None)[2]
    md_path = os.path.abspath(md_path)

    if not os.path.isdir(md_path):
        print(f"MD path does not exist: {md_path}", file=sys.stderr)
        sys.exit(1)

    with_yid, _ = yid_precheck.scan_md_files(md_path)
    jsonl_path = os.path.join(data_root, OFFLINE_JSONL)
    seen = load_existing_md_paths(jsonl_path)

    to_process = []
    for rel_path, date_folder, filename in with_yid:
        in_subdir = bool(date_folder)
        v_id = extract_vid_from_md(rel_path, filename, in_subdir)
        if not v_id or len(v_id) != 11:
            continue
        full_path = os.path.normpath(os.path.join(md_path, rel_path))
        if full_path in seen:
            continue
        transcript_date = date_folder.replace("_", "-") if date_folder else ""
        to_process.append((full_path, rel_path, v_id, transcript_date))

    print("=" * 60)
    print("Phase 1: 과거 영상 MD → video_metadata_offline.jsonl (YouTube API)")
    print("=" * 60)
    print(f"MD path: {md_path}")
    print(f"YID-present MDs: {len(with_yid)}")
    print(f"To process (excluding existing): {len(to_process)}")
    print(f"Output: {jsonl_path}")
    if args.dry_run:
        print("\n[DRY-RUN] No writes.")
        for i, (fp, rp, vid, td) in enumerate(to_process[:20]):
            print(f"  {i+1}. {rp} v_id={vid} transcript_date={td}")
        if len(to_process) > 20:
            print(f"  ... and {len(to_process) - 20} more")
        return
    print()

    # Batch fetch via YouTube API (50 per request)
    unique_vids = list(dict.fromkeys(t[2] for t in to_process))
    print(f"Fetching metadata for {len(unique_vids)} unique v_ids (YouTube API batch)...")
    metadata_map = fetch_video_metadata_batch(api_key, unique_vids)
    print(f"Fetched {len(metadata_map)} records")
    print()

    iterator = tqdm(to_process, desc="Writing offline JSONL", unit="md") if tqdm else to_process
    for i, (full_path, rel_path, v_id, transcript_date) in enumerate(iterator):
        meta = metadata_map.get(v_id, {})
        upload_date = meta.get("upload_date", "") or ""
        record = {
            "upload_date": upload_date,
            "v_id": v_id,
            "transcript_date": transcript_date,
            "method": "offline_phase1",
            "md_path": full_path,
            "has_yid": True,
        }
        append_offline_record(jsonl_path, record)
        seen.add(full_path)

        if (i + 1) % 50 == 0 and i > 0:
            time.sleep(BATCH_DELAY)

    print(f"\nDone. Appended {len(to_process)} records to {jsonl_path}")
    print("Run scripts/video_metadata_merge.py to merge live + offline.")


if __name__ == "__main__":
    main()
