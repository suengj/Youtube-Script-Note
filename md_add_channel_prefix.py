#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Add channel prefix to existing MD files that were processed via channel crawl.

Uses crawl_yt_list.csv (video_id -> channel_id/url) and channel_df.csv (channel_id/url -> usage_channel)
to rename files in YYYY_MM_DD/ folders from `파일명.md` to `{usage_channel}_파일명.md`.

Only renames files whose video_id is in the crawl queue (status=done).
Skips files that already have the channel prefix.

Usage:
  python md_add_channel_prefix.py [--base-path DIR] [--data-root DIR] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    import channel_crawl
except ImportError:
    channel_crawl = None
try:
    from config import resolve_data_root
except ImportError:
    resolve_data_root = None  # type: ignore

VID_PATTERN = re.compile(r"([A-Za-z0-9_-]{11})")
VID_BEFORE_SUFFIX = re.compile(r"_([A-Za-z0-9_-]{11})_(?:\d+-mini|ko_auto_subs|auto_subs|subs|en|jp)")

DEFAULT_BASE_PATH = str(Path(__file__).resolve().parent)
DEFAULT_MD_PATH = ""


def get_paths(
    args_base: Optional[str],
    args_data_root: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Return (base_path, md_path, data_root)."""
    base = args_base
    if not base or not os.path.isdir(base):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
        base = os.getenv("BASE_PATH", "").strip() or DEFAULT_BASE_PATH
    base = os.path.abspath(base)
    work = os.getenv("WORK_PATH", "").strip() or None
    if args_data_root:
        data_root = os.path.abspath(args_data_root)
    elif resolve_data_root:
        data_root = resolve_data_root(base, work)
    else:
        data_root = base
    md_path = os.getenv("OUTPUT_MD_PATH", "").strip() or DEFAULT_MD_PATH
    if not md_path:
        raise ValueError("OUTPUT_MD_PATH must be set in .env")
    md_path = os.path.abspath(md_path)
    return base, md_path, data_root


def sanitize_channel_name(name: str) -> str:
    """Sanitize channel name for filename (filesystem-safe chars only)."""
    if not name or not str(name).strip():
        return ""
    return re.sub(r'[/\\:*?"<>|]', '_', str(name).strip())


def extract_vid_from_md_filename(name: str) -> Optional[str]:
    """Extract YouTube video_id from MD filename."""
    if not name or not name.lower().endswith(".md"):
        return None
    base = name[:-3]
    if "+vid-" in base:
        return None
    m = VID_BEFORE_SUFFIX.search(base)
    if m:
        return m.group(1)
    matches = VID_PATTERN.findall(base)
    if matches:
        return matches[-1]
    return None


def load_channel_id_to_usage(base_path: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Load (channel_id -> usage_channel, channel_url -> usage_channel) from channel_df.csv."""
    path = os.path.join(base_path, channel_crawl.CHANNEL_DF_FILENAME) if channel_crawl else ""
    if not path or not os.path.exists(path):
        return {}, {}
    cid_to_usage: Dict[str, str] = {}
    url_to_usage: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url_s = (row.get("channel_url") or "").strip()
            usage = (row.get("usage_channel") or "").strip()
            if not url_s or not usage:
                continue
            url_to_usage[url_s] = usage
            if channel_crawl:
                cid = channel_crawl.extract_channel_id_from_url(url_s)
                if cid:
                    cid_to_usage[cid] = usage
    return cid_to_usage, url_to_usage


def load_vid_to_usage_channel(base_path: str) -> Dict[str, str]:
    """
    Build video_id -> usage_channel via:
    1. channel_df.csv: channel_id/url -> usage_channel
    2. crawl_yt_list.csv: video_id -> channel_id/url for status=done
    """
    if not channel_crawl:
        return {}
    cid_to_usage, url_to_usage = load_channel_id_to_usage(base_path)
    if not cid_to_usage and not url_to_usage:
        return {}

    path = os.path.join(base_path, channel_crawl.CRAWL_QUEUE_FILENAME)
    if not os.path.exists(path):
        return {}
    try:
        import pandas as pd
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        return {}

    done = channel_crawl.DONE_STATUSES | {channel_crawl.SHORTS_STATUS, channel_crawl.SKIPPED_AUTO_SUBS_ONLY_STATUS, "done"}
    result: Dict[str, str] = {}
    for _, row in df.iterrows():
        if str(row.get("status", "")).strip() not in done:
            continue
        vid = str(row.get("video_id", "")).strip()
        if not vid:
            continue
        cid = str(row.get("channel_id", "")).strip()
        url = str(row.get("channel_url", "")).strip()
        usage = cid_to_usage.get(cid) if cid else None
        if not usage and url:
            usage = url_to_usage.get(url)
        if usage:
            result[vid] = sanitize_channel_name(usage)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add channel prefix to MD files (crawl_yt_list + channel_df lookup)."
    )
    parser.add_argument("--base-path", type=str, default=None, help="Project base path (code, prompts)")
    parser.add_argument(
        "--data-root",
        type=str,
        default=None,
        help="Hot CSV dir (default: DATA_ROOT env or WORK_PATH/data or BASE_PATH)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned renames only")
    args = parser.parse_args()

    _base, md_path, data_root = get_paths(args.base_path, args.data_root)
    if not os.path.isdir(md_path):
        print(f"MD path does not exist: {md_path}", file=sys.stderr)
        sys.exit(1)

    vid_to_usage = load_vid_to_usage_channel(data_root)
    if not vid_to_usage:
        print("No video_id->usage_channel mapping (channel_df + crawl_yt_list).", file=sys.stderr)
        sys.exit(0)

    moved = 0
    skipped = 0

    for folder in sorted(os.listdir(md_path)):
        folder_path = os.path.join(md_path, folder)
        if not os.path.isdir(folder_path):
            continue
        if not re.match(r"^\d{4}_\d{2}_\d{2}$", folder):
            continue

        for name in sorted(os.listdir(folder_path)):
            if not name.lower().endswith(".md"):
                continue
            full_src = os.path.join(folder_path, name)
            if not os.path.isfile(full_src):
                continue

            vid = extract_vid_from_md_filename(name)
            if not vid or vid not in vid_to_usage:
                skipped += 1
                continue

            channel_prefix = vid_to_usage[vid]
            if not channel_prefix:
                continue

            if name.startswith(channel_prefix + "_"):
                continue

            new_name = f"{channel_prefix}_{name}"
            full_dest = os.path.join(folder_path, new_name)

            if os.path.exists(full_dest):
                print(f"Skip (dest exists): {folder}/{name} -> {new_name}", file=sys.stderr)
                skipped += 1
                continue

            if args.dry_run:
                print(f"Would rename: {folder}/{name} -> {folder}/{new_name}")
                moved += 1
            else:
                try:
                    os.rename(full_src, full_dest)
                    print(f"Renamed: {folder}/{name} -> {folder}/{new_name}")
                    moved += 1
                except Exception as e:
                    print(f"Error renaming {name}: {e}", file=sys.stderr)
                    skipped += 1

    if args.dry_run:
        print(f"[dry-run] Would rename {moved} file(s). Skipped: {skipped}")
    else:
        print(f"Done. Renamed: {moved}. Skipped: {skipped}")


if __name__ == "__main__":
    main()
