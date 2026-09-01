#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Compare note_catalog.jsonl with Obsidian MD filesystem (gap report).

Usage:
  python scripts/audit_note_catalog.py [--recent-days N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.note_catalog_utils import (  # noqa: E402
    audit_report_path,
    catalog_lookup_by_rel,
    catalog_path,
    extract_suffix_from_filename,
    extract_vid_from_filename,
    iter_md_files,
    load_catalog,
    recent_cutoff,
    resolve_paths,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit note catalog vs MD vault")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--work-path", type=str, default=None)
    parser.add_argument("--md-path", type=str, default=None)
    parser.add_argument("--recent-days", type=int, default=0, help="Limit MD scan to last N days (0=all)")
    args = parser.parse_args()

    base, work, data_root, md_root = resolve_paths(args.base_path, args.work_path, args.md_path)
    cat_file = catalog_path(work, data_root)
    if not os.path.isfile(cat_file):
        print(f"Catalog not found: {cat_file}. Run build_note_catalog.py first.", file=sys.stderr)
        sys.exit(1)

    catalog = load_catalog(cat_file)
    by_rel = catalog_lookup_by_rel(catalog)
    catalog_vids = {e.get("vid") for e in catalog if e.get("vid")}

    since = recent_cutoff(args.recent_days) if args.recent_days > 0 else None
    md_files = list(iter_md_files(md_root, since=since))
    md_rels = {rel for rel, _ in md_files}

    in_catalog_not_fs = sorted(set(by_rel.keys()) - md_rels)
    in_fs_not_catalog = sorted(md_rels - set(by_rel.keys()))
    in_both = md_rels & set(by_rel.keys())

    md_no_vid = []
    for rel, _ in md_files:
        if not extract_vid_from_filename(rel):
            md_no_vid.append(rel)

    suffix_counter = Counter()
    for rel in md_rels:
        suffix_counter[extract_suffix_from_filename(rel) or "(none)"] += 1

    report = {
        "md_root": md_root,
        "catalog_path": cat_file,
        "catalog_entries": len(catalog),
        "catalog_with_vid": len(catalog_vids),
        "md_files_scanned": len(md_files),
        "recent_days": args.recent_days,
        "in_both": len(in_both),
        "in_catalog_not_on_disk": len(in_catalog_not_fs),
        "on_disk_not_in_catalog": len(in_fs_not_catalog),
        "md_without_vid_in_filename": len(md_no_vid),
        "suffix_distribution": dict(suffix_counter.most_common(15)),
        "samples": {
            "catalog_not_disk": in_catalog_not_fs[:20],
            "disk_not_catalog": in_fs_not_catalog[:20],
            "no_vid": md_no_vid[:20],
        },
    }

    out_path = audit_report_path(work, data_root)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=" * 60)
    print("note_catalog audit")
    print("=" * 60)
    print(f"catalog entries:     {report['catalog_entries']}")
    print(f"md files scanned:    {report['md_files_scanned']}")
    print(f"in both:             {report['in_both']}")
    print(f"catalog not on disk: {report['in_catalog_not_on_disk']}")
    print(f"disk not in catalog: {report['on_disk_not_in_catalog']}")
    print(f"md no vid in name:   {report['md_without_vid_in_filename']}")
    print(f"report:              {out_path}")


if __name__ == "__main__":
    main()
