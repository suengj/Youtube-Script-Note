#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build YTT_AUDIO/index/note_catalog.jsonl from local JSONL + output_df (no LLM, no full MD scan).

Usage:
  python scripts/build_note_catalog.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.note_catalog_utils import (  # noqa: E402
    build_catalog,
    catalog_path,
    index_dir,
    resolve_paths,
    write_catalog,
)


def ensure_digest_folder(md_root: str) -> str:
    digest = os.path.join(md_root, "digest")
    os.makedirs(digest, exist_ok=True)
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build note_catalog.jsonl")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--work-path", type=str, default=None)
    parser.add_argument("--md-path", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    base, work, data_root, md_root = resolve_paths(args.base_path, args.work_path, args.md_path)
    entries = build_catalog(base, work, md_root)
    out_path = catalog_path(work, data_root)
    digest = ensure_digest_folder(md_root)

    with_vid = sum(1 for e in entries if e.get("vid"))
    with_rel = sum(1 for e in entries if e.get("md_path_rel"))

    print("=" * 60)
    print("Build note_catalog.jsonl")
    print("=" * 60)
    print(f"data_root: {data_root}")
    print(f"md_root:   {md_root}")
    print(f"index:     {index_dir(work, data_root)}")
    print(f"digest:    {digest}")
    print(f"entries:   {len(entries)} (vid={with_vid}, md_path_rel={with_rel})")
    print(f"output:    {out_path}")

    if args.dry_run:
        for e in entries[:5]:
            print(json.dumps(e, ensure_ascii=False)[:200])
        if len(entries) > 5:
            print(f"... and {len(entries) - 5} more")
        return

    write_catalog(entries, out_path)
    print(f"Wrote {len(entries)} rows -> {out_path}")


if __name__ == "__main__":
    main()
