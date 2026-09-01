#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build digest/YYYY_MM_DD.md from catalog or same-day MD files (LLM $0).

Usage:
  python scripts/build_daily_digest.py
  python scripts/build_daily_digest.py --date 2026-06-28 --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.md_mobile_utils import extract_tldr, extract_title  # noqa: E402
from scripts.note_catalog_utils import (  # noqa: E402
    atomic_write_text_with_retry,
    catalog_path,
    extract_channel_from_filename,
    has_frontmatter,
    iter_md_files,
    load_catalog,
    parse_date_folder,
    resolve_paths,
    strip_leading_frontmatter,
)


def _wikilink(rel: str) -> str:
    if not rel:
        return ""
    name = os.path.splitext(os.path.basename(rel))[0]
    return f"[[{name}]]"


def _one_line(text: str, max_len: int = 120) -> str:
    t = " ".join((text or "").split())
    if len(t) > max_len:
        return t[: max_len - 1] + "…"
    return t


def entries_for_date(catalog: List[Dict[str, Any]], date_str: str) -> List[Dict[str, Any]]:
    out = []
    for e in catalog:
        td = (e.get("transcript_date") or e.get("upload_date") or "")[:10]
        if td == date_str and e.get("md_path_rel"):
            out.append(e)
    return out


def scan_md_folder(md_root: str, date_str: str) -> List[Dict[str, Any]]:
    folder = date_str.replace("-", "_")
    since_dt = parse_date_folder(folder)
    if since_dt is None:
        return []
    entries: List[Dict[str, Any]] = []
    for rel, abs_path in iter_md_files(md_root, since=since_dt):
        if not rel.startswith(f"{folder}/") or rel.startswith("digest/"):
            continue
        try:
            with open(abs_path, "r", encoding="utf-8-sig") as f:
                raw = f.read()
        except OSError:
            continue
        body = strip_leading_frontmatter(raw) if has_frontmatter(raw) else raw
        title = extract_title(body, fallback=os.path.splitext(os.path.basename(rel))[0])
        tldr = extract_tldr(body)
        fname = os.path.basename(rel)
        entries.append(
            {
                "md_path_rel": rel,
                "channel": extract_channel_from_filename(fname),
                "title": title,
                "tldr": tldr,
            }
        )
    return entries


def render_digest(date_str: str, rows: List[Dict[str, Any]]) -> str:
    folder = date_str.replace("-", "_")
    lines = [
        f"# Daily digest — {date_str}",
        "",
        f"신규 노트 {len(rows)}건 (`002_YT_Script/{folder}/`)",
        "",
        "| 채널 | 제목 | 한눈에 보기 | 링크 |",
        "| --- | --- | --- | --- |",
    ]
    for e in sorted(rows, key=lambda x: x.get("channel") or ""):
        ch = e.get("channel") or "—"
        title = _one_line(e.get("title") or "—", 60)
        tldr = _one_line(e.get("tldr") or "—", 80)
        link = _wikilink(e.get("md_path_rel") or "")
        lines.append(f"| {ch} | {title} | {tldr} | {link} |")
    lines.append("")
    return "\n".join(lines)


def build_digest(
    md_root: str,
    work_path: str,
    data_root: str,
    date_str: str,
    *,
    dry_run: bool = False,
) -> str:
    cat_file = catalog_path(work_path, data_root)
    catalog = load_catalog(cat_file) if os.path.isfile(cat_file) else []
    rows = entries_for_date(catalog, date_str)
    if not rows:
        rows = scan_md_folder(md_root, date_str)
    content = render_digest(date_str, rows)
    out_rel = f"digest/{date_str.replace('-', '_')}.md"
    out_path = os.path.join(md_root, out_rel)
    if dry_run:
        print(content[:800])
        if len(content) > 800:
            print("...")
        print(f"\nWould write: {out_path} ({len(rows)} rows)")
        return out_path
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    atomic_write_text_with_retry(out_path, content)
    print(f"Wrote {out_path} ({len(rows)} rows)")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build daily Obsidian digest")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--work-path", type=str, default=None)
    parser.add_argument("--md-path", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    base, work, data_root, md_root = resolve_paths(args.base_path, args.work_path, args.md_path)
    build_digest(md_root, work, data_root, date_str, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
