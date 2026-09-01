#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prepend YAML frontmatter to recent MD files using note_catalog (LLM $0).

Usage:
  python scripts/backfill_frontmatter_recent.py --days 30 --dry-run
  python scripts/backfill_frontmatter_recent.py --days 30 --apply
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

from scripts.note_catalog_utils import (  # noqa: E402
    atomic_write_text_with_retry,
    build_frontmatter_yaml,
    catalog_lookup_by_rel,
    catalog_lookup_by_vid,
    catalog_path,
    enrich_entry_from_path,
    has_frontmatter,
    iter_md_files,
    load_catalog,
    recent_cutoff,
    resolve_paths,
    strip_leading_frontmatter,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill YAML frontmatter on recent MD files")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--work-path", type=str, default=None)
    parser.add_argument("--md-path", type=str, default=None)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true", help="Plan only (default if --apply omitted)")
    parser.add_argument("--apply", action="store_true", help="Write files")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing v4.0 frontmatter (fixes bad vid extraction)",
    )
    args = parser.parse_args()

    if not args.apply:
        args.dry_run = True

    base, work, data_root, md_root = resolve_paths(args.base_path, args.work_path, args.md_path)
    cat_file = catalog_path(work, data_root)
    catalog = load_catalog(cat_file) if os.path.isfile(cat_file) else []
    by_rel = catalog_lookup_by_rel(catalog)
    by_vid = catalog_lookup_by_vid(catalog)

    since = recent_cutoff(args.days)
    candidates = []
    skipped_frontmatter = 0
    for rel, abs_path in iter_md_files(md_root, since=since):
        if rel.startswith("digest/"):
            continue
        try:
            with open(abs_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
        except OSError:
            continue
        if has_frontmatter(content):
            if not args.force:
                skipped_frontmatter += 1
                continue
            content = strip_leading_frontmatter(content)
        entry = by_rel.get(rel)
        if entry is None:
            vid = enrich_entry_from_path(rel, abs_path).get("vid")
            if vid and vid in by_vid:
                entry = dict(by_vid[vid])
        entry = enrich_entry_from_path(rel, abs_path, entry)
        candidates.append((rel, abs_path, content, entry))

    print("=" * 60)
    print("Backfill frontmatter (Phase 1.5)")
    print("=" * 60)
    print(f"md_root:   {md_root}")
    print(f"catalog:   {cat_file} ({len(catalog)} rows)")
    print(f"since:     {since.date()} ({args.days} days)")
    print(f"skipped (already ---): {skipped_frontmatter}")
    print(f"to update: {len(candidates)}")
    print(f"mode:      {'APPLY' if args.apply else 'DRY-RUN'}")

    if args.dry_run and not args.apply:
        for rel, _, _, entry in candidates[:10]:
            print(f"  {rel} vid={entry.get('vid')} channel={entry.get('channel')}")
        if len(candidates) > 10:
            print(f"  ... and {len(candidates) - 10} more")
        return

    ok = 0
    fail = 0
    iterator = candidates
    if tqdm:
        iterator = tqdm(candidates, desc="frontmatter", unit="md")
    for rel, abs_path, content, entry in iterator:
        fm = build_frontmatter_yaml(entry)
        new_content = fm + content
        try:
            atomic_write_text_with_retry(abs_path, new_content)
            ok += 1
        except OSError as e:
            fail += 1
            print(f"\nFailed {rel}: {e}", file=sys.stderr)

    print(f"\nDone. updated={ok} failed={fail}")


if __name__ == "__main__":
    main()
