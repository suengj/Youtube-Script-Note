#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-time relocate: move top-level .md files in the Obsidian path into date subfolders.

Pattern: YYYY-MM-DD_파일명.md or YYYY_MM_DD_파일명.md → YYYY_MM_DD/파일명.md
Only top-level .md files are processed; files already inside date folders are untouched.

Usage:
  python md_relocate.py [--path DIR] [--dry-run]
  --path   Target directory (default: OUTPUT_MD_PATH from .env or built-in default)
  --dry-run  Print planned moves only; do not create folders or move files
"""

import argparse
import os
import re
import shutil
import sys
from typing import Optional, Tuple

DEFAULT_OBSIDIAN_PATH = ""

# Top-level .md: YYYY-MM-DD_rest or YYYY_MM_DD_rest → (YYYY_MM_DD, rest.md)
PATTERN_HYPHEN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(.+)\.md$", re.IGNORECASE)
PATTERN_UNDERSCORE = re.compile(r"^(\d{4})_(\d{2})_(\d{2})_(.+)\.md$", re.IGNORECASE)


def parse_date_prefix(basename: str) -> Tuple[Optional[str], Optional[str]]:
    """
    If basename matches a date-prefix pattern, return (YYYY_MM_DD_folder, filename_with_ext).
    Otherwise return (None, None). filename is the part after the date prefix, e.g. 'rest.md'.
    """
    for pattern in (PATTERN_HYPHEN, PATTERN_UNDERSCORE):
        m = pattern.match(basename)
        if m:
            y, mo, d, rest = m.groups()
            folder = f"{y}_{mo}_{d}"
            new_name = rest if rest.lower().endswith(".md") else rest + ".md"
            return folder, new_name
    return None, None


def get_target_dir(args_path: Optional[str]) -> str:
    """Resolve target directory: CLI --path > .env OUTPUT_MD_PATH > default."""
    if args_path and os.path.isdir(args_path):
        return os.path.abspath(args_path)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    env_path = os.getenv("OUTPUT_MD_PATH", "").strip()
    if env_path and os.path.isdir(env_path):
        return os.path.abspath(env_path)
    if DEFAULT_OBSIDIAN_PATH:
        return os.path.abspath(DEFAULT_OBSIDIAN_PATH)
    raise ValueError("Set OUTPUT_MD_PATH in .env or pass --path")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Relocate top-level .md files into YYYY_MM_DD/파일명.md in the Obsidian path."
    )
    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Target directory (default: OUTPUT_MD_PATH from .env or built-in default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned moves only; do not create folders or move files",
    )
    args = parser.parse_args()

    target_dir = get_target_dir(args.path)
    if not os.path.isdir(target_dir):
        print(f"Error: target directory does not exist: {target_dir}", file=sys.stderr)
        sys.exit(1)

    # Only top-level .md files (no recursion)
    moved = 0
    skipped_collision = 0
    skipped_no_match = 0

    for name in sorted(os.listdir(target_dir)):
        if not name.lower().endswith(".md"):
            continue
        full_src = os.path.join(target_dir, name)
        if not os.path.isfile(full_src):
            continue

        folder, new_name = parse_date_prefix(name)
        if folder is None or new_name is None:
            skipped_no_match += 1
            continue

        date_dir = os.path.join(target_dir, folder)
        dest = os.path.join(date_dir, new_name)

        if os.path.exists(dest):
            print(f"Skip (dest exists): {name} -> {folder}/{new_name}")
            skipped_collision += 1
            continue

        if args.dry_run:
            print(f"Would move: {name} -> {folder}/{new_name}")
            moved += 1
            continue

        os.makedirs(date_dir, exist_ok=True)
        shutil.move(full_src, dest)
        print(f"Moved: {name} -> {folder}/{new_name}")
        moved += 1

    if args.dry_run:
        print(f"[dry-run] Would move {moved} file(s). Skipped (no match): {skipped_no_match}, (dest exists): {skipped_collision}")
    else:
        print(f"Done. Moved: {moved}. Skipped (dest exists): {skipped_collision}. No match: {skipped_no_match}")


if __name__ == "__main__":
    main()
