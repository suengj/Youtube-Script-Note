#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill Obsidian callout `>` prefixes on saved MD files (no LLM)."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from scripts.md_mobile_utils import audit_callout_prefixes, normalize_obsidian_callouts  # noqa: E402

FM_END = re.compile(r"^---\s*\n.*?\n---\s*\n", re.S)


def atomic_write_text(path: Path, content: str) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".backfill_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(content)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def split_frontmatter(text: str) -> tuple[str, str]:
    m = FM_END.match(text)
    if not m:
        return "", text
    return m.group(0), text[m.end() :]


def process_file(path: Path, *, dry_run: bool, verbose: bool) -> str:
    raw = path.read_text(encoding="utf-8-sig")
    fm, body = split_frontmatter(raw)
    if not fm:
        return "skip_no_frontmatter"
    before_issues = audit_callout_prefixes(body)
    if not before_issues:
        return "skip_ok"
    new_body = normalize_obsidian_callouts(body)
    after_issues = audit_callout_prefixes(new_body)
    if new_body == body:
        return "skip_unchanged"
    if after_issues and verbose:
        print(f"  still broken after fix: {path.name} ({len(after_issues)} lines)")
    new_text = fm + new_body
    if dry_run:
        print(f"  would fix: {path.name} ({len(before_issues)} bad lines)")
        return "would_fix"
    atomic_write_text(path, new_text)
    if verbose:
        print(f"  fixed: {path.name} ({len(before_issues)} bad lines)")
    return "fixed"


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Obsidian callout blockquote prefixes")
    parser.add_argument(
        "--folder",
        default="2026_07_06",
        help="Date folder under OUTPUT_MD_PATH (default: 2026_07_06)",
    )
    parser.add_argument("--glob", default="*_5-mini*.md", help="Filename glob within folder")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    md_root = os.getenv("OUTPUT_MD_PATH", "").strip()
    if not md_root:
        print("OUTPUT_MD_PATH must be set in .env", file=sys.stderr)
        sys.exit(1)
    target_dir = Path(md_root) / args.folder
    if not target_dir.is_dir():
        print(f"Not found: {target_dir}", file=sys.stderr)
        sys.exit(1)

    files = sorted(target_dir.glob(args.glob))
    if not files:
        print(f"No files matching {args.glob} in {target_dir}", file=sys.stderr)
        sys.exit(1)

    counts: dict[str, int] = {}
    for f in files:
        status = process_file(f, dry_run=args.dry_run, verbose=args.verbose)
        counts[status] = counts.get(status, 0) + 1

    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"{mode} {target_dir} ({len(files)} files): {counts}")


if __name__ == "__main__":
    main()
