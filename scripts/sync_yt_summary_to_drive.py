#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sync finalized P03 Obsidian YouTube summaries to Google Drive Desktop YT_summary (SUE-401).

Filesystem transport: copies into the local Google Drive Desktop mount; cloud sync is automatic.

Usage:
  python scripts/sync_yt_summary_to_drive.py --dry-run
  python scripts/sync_yt_summary_to_drive.py --limit 3
  python scripts/sync_yt_summary_to_drive.py --backfill-date 2026-08-31
  python scripts/sync_yt_summary_to_drive.py --migrate-legacy-only
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.drive_yt_summary.config import (  # noqa: E402
    DriveSyncConfigError,
    discover_yt_summary_root,
    load_config,
)
from scripts.drive_yt_summary.legacy import migrate_contents_gen_to_legacy  # noqa: E402
from scripts.drive_yt_summary.sync import run_sync  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync YT summaries to Google Drive Desktop YT_summary (filesystem)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan actions without writes")
    parser.add_argument("--limit", type=int, default=None, help="Max local files to sync (testing)")
    parser.add_argument(
        "--date-folder",
        type=str,
        default=None,
        help="Sync only one date folder (YYYY_MM_DD or YYYY-MM-DD)",
    )
    parser.add_argument(
        "--backfill-date",
        type=str,
        default=None,
        help="Bounded backfill for one calendar date (YYYY-MM-DD or YYYY_MM_DD)",
    )
    parser.add_argument(
        "--sync-root",
        type=str,
        default=None,
        help="Override P03_DRIVE_SYNC_ROOT (Google Drive Desktop YT_summary path)",
    )
    parser.add_argument(
        "--migrate-legacy-only",
        action="store_true",
        help="Only move contents_gen under legacy/ (no source sync)",
    )
    parser.add_argument("--no-migrate-legacy", action="store_true", help="Skip legacy migration step")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--work-path", type=str, default=None)
    parser.add_argument("--md-path", type=str, default=None)
    args = parser.parse_args()

    try:
        config = load_config(
            args.base_path,
            args.work_path,
            args.md_path,
            sync_root=args.sync_root,
        )
    except DriveSyncConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        discovered = discover_yt_summary_root()
        if discovered:
            print(f"discovered_yt_summary: {discovered}")
        return 2

    print("=" * 60)
    print("P03 → Google Drive Desktop YT_summary sync (SUE-401)")
    print("=" * 60)
    print(f"md_root:              {config.md_root}")
    print(f"sync_root:            {config.sync_root}")
    print(f"source_dir:           {config.source_dir}")
    print(f"state:                {config.state_path}")
    print(f"enabled:              {config.enabled}")
    print(f"dry_run:              {args.dry_run}")
    if args.limit is not None:
        print(f"limit:                {args.limit}")
    if args.date_folder is not None:
        print(f"date_folder:          {args.date_folder}")
    if args.backfill_date is not None:
        print(f"backfill_date:        {args.backfill_date}")

    if args.migrate_legacy_only:
        if args.dry_run:
            res = migrate_contents_gen_to_legacy(config.sync_root, dry_run=True)
            print(res.message)
            return 0
        res = migrate_contents_gen_to_legacy(config.sync_root, dry_run=False)
        print(res.message)
        return 0

    result = run_sync(
        dry_run=args.dry_run,
        migrate_legacy=not args.no_migrate_legacy,
        limit=args.limit,
        date_folder=args.date_folder,
        backfill_date=args.backfill_date,
        base_path=args.base_path,
        work_path=args.work_path,
        md_path=args.md_path,
        sync_root=args.sync_root,
    )

    if result.legacy_message:
        print(f"legacy: {result.legacy_message}")
    if result.sync_root:
        print(f"sync_root: {result.sync_root}")
    if result.source_dir:
        print(f"source_dir: {result.source_dir}")
    if result.manifest_path:
        print(f"manifest_path: {result.manifest_path}")

    print(
        f"summary: created={result.created} updated={result.updated} "
        f"skipped={result.skipped} errors={result.errors}"
    )
    for action in result.actions[:50]:
        print(f"  - {action}")
    if len(result.actions) > 50:
        print(f"  ... and {len(result.actions) - 50} more")
    for err in result.error_messages:
        print(f"ERROR: {err}")

    return 0 if result.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
