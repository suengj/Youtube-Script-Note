#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Batch rename: VID-only filenames → YouTube title + VID.

Finds files in output_new/summary/ and OUTPUT_MD_PATH whose base name is VID-only
(e.g. xHi8PUIVyoo_auto_subs_5-mini.txt, 2026-03-08_xHi8PUIVyoo_auto_subs_5-mini.md),
fetches YouTube title for each VID, and renames to {title}_{VID}_*.

Usage:
  python vid_to_title_rename.py [--base-path DIR] [--dry-run]
  --base-path  Project base (default: BASE_PATH from .env or built-in)
  --dry-run    Print planned renames only; do not rename
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# YouTube ID: 11 chars, alphanumeric + -_
VID_PATTERN = re.compile(r"^([A-Za-z0-9_-]{11})(_.*)$")
# MD: YYYY-MM-DD_ or YYYY_MM_DD_
MD_DATE_PREFIX = re.compile(r"^(\d{4})[-_](\d{2})[-_](\d{2})_(.+)$")
# Date subfolder: YYYY_MM_DD
DATE_FOLDER_PATTERN = re.compile(r"^(\d{4})_(\d{2})_(\d{2})$")

DEFAULT_BASE_PATH = str(Path(__file__).resolve().parent)
DEFAULT_MD_PATH = ""


def get_paths(args_base: str | None) -> tuple[str, str, str]:
    """Return (base_path, summary_path, md_path)."""
    base = args_base
    if not base or not os.path.isdir(base):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
        base = os.getenv("BASE_PATH", "").strip() or DEFAULT_BASE_PATH
    base = os.path.abspath(base)
    summary = os.path.join(base, "output_new", "summary")
    md_path = os.getenv("OUTPUT_MD_PATH", "").strip() or DEFAULT_MD_PATH
    if not md_path:
        raise ValueError("OUTPUT_MD_PATH must be set in .env")
    md_path = os.path.abspath(md_path)
    return base, summary, md_path


def extract_vid_from_summary_name(name: str) -> tuple[str | None, str | None]:
    """
    If name is VID_suffix.txt (VID-only, no title), return (vid, suffix_with_ext).
    Skip files that already have title (e.g. contain +vid- from Whisper path).
    Otherwise (None, None).
    """
    if not name.endswith(".txt"):
        return None, None
    base = name[:-4]  # without .txt
    # Skip Whisper-format: Title+vid-VID_suffix (already has title)
    if "+vid-" in base:
        return None, None
    m = VID_PATTERN.match(base)
    if m:
        return m.group(1), m.group(2) + ".txt"
    return None, None


def extract_vid_from_md_name(name: str) -> tuple[str | None, str | None, str | None]:
    """
    If name is YYYY-MM-DD_VID_suffix.md (flat), return (date_prefix, vid, suffix_with_ext).
    date_prefix is e.g. "2026-03-08".
    """
    if not name.lower().endswith(".md"):
        return None, None, None
    m = MD_DATE_PREFIX.match(name)
    if not m:
        return None, None, None
    y, mo, d, rest = m.groups()
    date_prefix = f"{y}-{mo}-{d}"
    # rest = VID_suffix.md; skip if already has title (e.g. +vid- from Whisper)
    if "+vid-" in rest:
        return None, None, None
    m2 = VID_PATTERN.match(rest)
    if m2:
        return date_prefix, m2.group(1), m2.group(2)
    return None, None, None


def extract_vid_from_md_in_subdir(name: str) -> tuple[str | None, str | None]:
    """
    If name is VID_suffix.md or 채널명_VID_suffix.md (in date subfolder), return (vid, suffix_with_ext).
    Skip files with +vid- (Whisper format).
    """
    if not name.lower().endswith(".md"):
        return None, None
    base = name[:-3]  # without .md
    if "+vid-" in base:
        return None, None
    # Try VID at start (legacy)
    m = VID_PATTERN.match(base)
    if m:
        return m.group(1), m.group(2) + ".md"
    # Try optional prefix (채널명_ or Title_): prefix_VID_suffix
    m2 = re.match(r"^(.+)_([A-Za-z0-9_-]{11})(_.*)$", base)
    if m2:
        _prefix, vid, suffix = m2.groups()
        if len(vid) == 11 and ("5-mini" in suffix or "auto_subs" in suffix or "subs" in suffix):
            return vid, suffix + ".md"
    return None, None


def extract_prefix_vid_suffix_from_md_in_subdir(name: str) -> tuple[str | None, str | None, str | None]:
    """
    If name is VID_suffix.md or prefix_VID_suffix.md, return (prefix_or_empty, vid, suffix_with_ext).
    prefix is empty for VID-only files. Used to preserve channel prefix when adding title.
    """
    if not name.lower().endswith(".md"):
        return None, None, None
    base = name[:-3]
    if "+vid-" in base:
        return None, None, None
    m = VID_PATTERN.match(base)
    if m:
        return "", m.group(1), m.group(2) + ".md"
    m2 = re.match(r"^(.+)_([A-Za-z0-9_-]{11})(_.*)$", base)
    if m2:
        prefix, vid, suffix = m2.groups()
        if len(vid) == 11 and ("5-mini" in suffix or "auto_subs" in suffix or "subs" in suffix):
            return prefix + "_", vid, suffix + ".md"
    return None, None, None


def fetch_youtube_title(video_id: str) -> str | None:
    """Fetch video title via yt-dlp. Returns None on failure."""
    try:
        import yt_dlp
    except ImportError:
        return None
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": False}) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("title") if info else None
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename VID-only files to include YouTube title in filename."
    )
    parser.add_argument(
        "--base-path",
        type=str,
        default=None,
        help="Project base path (default: BASE_PATH from .env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned renames only; do not rename",
    )
    args = parser.parse_args()

    base_path, summary_path, md_path = get_paths(args.base_path)
    if not os.path.isdir(summary_path):
        print(f"Summary path does not exist: {summary_path}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(md_path):
        print(f"MD path does not exist: {md_path}", file=sys.stderr)
        sys.exit(1)

    def _sanitize(s: str, max_len: int = 50) -> str:
        invalid = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
        out = invalid.sub("_", s).strip(" ").rstrip(".")
        if max_len > 0 and len(out) > max_len:
            out = out[:max_len]
        return out

    vid_to_title: dict[str, str] = {}
    renames: list[tuple[str, str]] = []

    # Scan summary
    for name in os.listdir(summary_path):
        vid, suffix = extract_vid_from_summary_name(name)
        if vid is None:
            continue
        full_src = os.path.join(summary_path, name)
        if not os.path.isfile(full_src):
            continue
        title = vid_to_title.get(vid)
        if title is None:
            title = fetch_youtube_title(vid)
            if not title:
                print(f"Skip (title not found): {name}", file=sys.stderr)
                continue
            vid_to_title[vid] = title
        safe_title = _sanitize(title, 50)
        base_part = safe_title.rsplit(".", 1)[0] if "." in safe_title else safe_title
        new_name = f"{base_part}_{vid}{suffix}"
        new_path = os.path.join(summary_path, new_name)
        if os.path.exists(new_path) and new_path != full_src:
            print(f"Skip (dest exists): {name} -> {new_name}", file=sys.stderr)
            continue
        renames.append((full_src, new_path))

    # Scan MD path: (1) top-level legacy YYYY-MM-DD_VID_suffix.md, (2) date subfolders YYYY_MM_DD/VID_suffix.md
    for name in os.listdir(md_path):
        full_src = os.path.join(md_path, name)
        if os.path.isfile(full_src):
            date_prefix, vid, suffix = extract_vid_from_md_name(name)
            if vid is not None:
                title = vid_to_title.get(vid)
                if title is None:
                    title = fetch_youtube_title(vid)
                    if not title:
                        print(f"Skip (title not found): {name}", file=sys.stderr)
                        continue
                    vid_to_title[vid] = title
                safe_title = _sanitize(title, 50)
                base_part = safe_title.rsplit(".", 1)[0] if "." in safe_title else safe_title
                # Legacy flat → move to date folder: YYYY_MM_DD/Title_VID_suffix.md
                date_folder = date_prefix.replace("-", "_")
                date_dir = os.path.join(md_path, date_folder)
                new_name = f"{base_part}_{vid}{suffix}"
                new_path = os.path.join(date_dir, new_name)
                if os.path.exists(new_path) and new_path != full_src:
                    print(f"Skip (dest exists): {name} -> {date_folder}/{new_name}", file=sys.stderr)
                    continue
                renames.append((full_src, new_path))
        elif os.path.isdir(full_src) and DATE_FOLDER_PATTERN.match(name):
            # Date subfolder: YYYY_MM_DD/VID_suffix.md or YYYY_MM_DD/채널명_VID_suffix.md → Title or 채널명_Title_VID_suffix
            for fname in os.listdir(full_src):
                prefix, vid, suffix = extract_prefix_vid_suffix_from_md_in_subdir(fname)
                if vid is None:
                    continue
                file_src = os.path.join(full_src, fname)
                if not os.path.isfile(file_src):
                    continue
                title = vid_to_title.get(vid)
                if title is None:
                    title = fetch_youtube_title(vid)
                    if not title:
                        print(f"Skip (title not found): {name}/{fname}", file=sys.stderr)
                        continue
                    vid_to_title[vid] = title
                safe_title = _sanitize(title, 50)
                base_part = safe_title.rsplit(".", 1)[0] if "." in safe_title else safe_title
                new_name = f"{prefix}{base_part}_{vid}{suffix}"
                new_path = os.path.join(full_src, new_name)
                if os.path.exists(new_path) and new_path != file_src:
                    print(f"Skip (dest exists): {name}/{fname} -> {new_name}", file=sys.stderr)
                    continue
                renames.append((file_src, new_path))

    # Execute renames
    for src, dest in renames:
        if args.dry_run:
            print(f"Would rename: {src} -> {dest}")
        else:
            dest_dir = os.path.dirname(dest)
            if dest_dir and not os.path.isdir(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)
            os.rename(src, dest)
            print(f"Renamed: {os.path.basename(src)} -> {os.path.basename(dest)}")

    if args.dry_run:
        print(f"[dry-run] Would rename {len(renames)} file(s)")
    else:
        print(f"Done. Renamed {len(renames)} file(s)")


if __name__ == "__main__":
    main()
