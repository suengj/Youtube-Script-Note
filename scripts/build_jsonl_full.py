#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
전체 JSONL 구축 파이프라인: Phase 1 → build-cache → Phase 2 → merge.

과거 영상 + YID-less → offline JSONL 구축 후, live와 merge하여
video_metadata_merged.jsonl 생성. (MD 본문 업데이트는 md_add_upload_date_header.py 별도 실행)

Usage:
  python scripts/build_jsonl_full.py [--base-path DIR] [--md-path DIR]
"""

from __future__ import annotations

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)


def run(cmd: list[str], desc: str) -> bool:
    """Run command, return True on success."""
    print("\n" + "=" * 60)
    print(desc)
    print("=" * 60)
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        print(f"\n[FAILED] {desc} (exit {result.returncode})", file=sys.stderr)
        return False
    return True


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Full JSONL build: Phase1 → cache → Phase2 → merge")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--md-path", type=str, default=None)
    parser.add_argument("--with-header", action="store_true", help="Step 5: md_add_upload_date_header 실행")
    args = parser.parse_args()

    def _arg(name: str, val: str | None) -> list[str]:
        return [name, val] if val else []

    steps = [
        (["python", "scripts/md_to_offline_jsonl_phase1.py"] + _arg("--base-path", args.base_path) + _arg("--md-path", args.md_path),
         "Phase 1: 과거 영상 MD → offline JSONL"),
        (["python", "scripts/yid_precheck.py", "--build-cache"] + _arg("--base-path", args.base_path),
         "Step 2: Title Cache 구축"),
        (["python", "scripts/yid_precheck.py", "--use-cache", "--write-offline-jsonl", "--sample", "0"] + _arg("--base-path", args.base_path) + _arg("--md-path", args.md_path),
         "Step 3: YID-less → offline JSONL"),
        (["python", "scripts/video_metadata_merge.py"] + _arg("--base-path", args.base_path),
         "Step 4: Merge live + offline → merged"),
    ]
    if args.with_header:
        steps.append((
            ["python", "scripts/md_add_upload_date_header.py"] + _arg("--base-path", args.base_path),
            "Step 5: MD 본문 상단에 영상 업로드 일자 추가",
        ))

    for cmd, desc in steps:
        if not run(cmd, desc):
            sys.exit(1)

    print("\n" + "=" * 60)
    print("Done. video_metadata_merged.jsonl ready.")
    if not args.with_header:
        print("MD 본문 업데이트: python scripts/md_add_upload_date_header.py")
        print("  또는 build_jsonl_full.py --with-header")
    print("=" * 60)


if __name__ == "__main__":
    main()
