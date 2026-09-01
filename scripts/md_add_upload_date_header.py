#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video_metadata_merged.jsonl 기반으로 MD 파일 본문 최상단에
'영상 업로드 일자: YYYY-MM-DD' 추가.

이미 헤더가 있으면 스킵. upload_date 없으면 스킵.

Usage:
  python scripts/md_add_upload_date_header.py [--base-path DIR] [--dry-run]
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

try:
    from config import resolve_data_root
except ImportError:
    def resolve_data_root(bp, wp=None):
        return os.path.abspath(bp)

MERGED_JSONL = "video_metadata_merged.jsonl"
HEADER_PATTERN = re.compile(r"^영상\s+업로드\s+일자\s*:\s*.+", re.MULTILINE)


def _has_upload_header(content: str) -> bool:
    """Check if content already has 영상 업로드 일자 in first 5 lines."""
    lines = content.split("\n")[:5]
    for line in lines:
        if "영상" in line and "업로드" in line and "일자" in line:
            return True
    return False


def _prepend_upload_header(content: str, upload_date: str) -> str:
    """Prepend '영상 업로드 일자: YYYY-MM-DD' to content."""
    header = f"영상 업로드 일자: {upload_date}\n\n"
    return header + content


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="MD 본문 상단에 영상 업로드 일자 추가")
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
    jsonl_path = os.path.join(data_root, MERGED_JSONL)
    if not os.path.isfile(jsonl_path):
        jsonl_path = os.path.join(base, MERGED_JSONL)
    if not os.path.isfile(jsonl_path):
        print(f"{MERGED_JSONL} not found. Run build_jsonl_full.py or video_metadata_merge.py first.", file=sys.stderr)
        sys.exit(1)

    records = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                md_path = rec.get("md_path", "")
                upload_date = (rec.get("upload_date") or "").strip()
                if md_path and upload_date and re.match(r"\d{4}-\d{2}-\d{2}", upload_date):
                    records.append((os.path.normpath(md_path), upload_date))
            except json.JSONDecodeError:
                continue

    to_update = []
    for md_path, upload_date in records:
        if not os.path.isfile(md_path):
            continue
        try:
            with open(md_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
        except Exception:
            continue
        if _has_upload_header(content):
            continue
        to_update.append((md_path, upload_date, content))

    print("=" * 60)
    print("MD Add Upload Date Header")
    print("=" * 60)
    print(f"JSONL: {jsonl_path}")
    print(f"Records with upload_date: {len(records)}")
    print(f"To update (no header yet): {len(to_update)}")

    if args.dry_run:
        print("\n[DRY-RUN] No writes.")
        for md_path, ud, _ in to_update[:10]:
            print(f"  {md_path} -> 영상 업로드 일자: {ud}")
        if len(to_update) > 10:
            print(f"  ... and {len(to_update) - 10} more")
        return

    iterator = tqdm(to_update, desc="Adding header", unit="md") if tqdm else to_update
    for md_path, upload_date, content in iterator:
        new_content = _prepend_upload_header(content, upload_date)
        try:
            with open(md_path, "w", encoding="utf-8-sig") as f:
                f.write(new_content)
        except Exception as e:
            print(f"\nFailed {md_path}: {e}", file=sys.stderr)

    print(f"\nDone. Updated {len(to_update)} MD files.")


if __name__ == "__main__":
    main()
