#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scan local Obsidian output for finalized YouTube summary Markdown."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from scripts.note_catalog_utils import (
    DATE_FOLDER_PATTERN,
    has_frontmatter,
    strip_leading_frontmatter,
)
from scripts.md_mobile_utils import extract_title


@dataclass(frozen=True)
class LocalSummaryFile:
    relative_path: str
    absolute_path: str
    drive_name: str
    title: str
    content: str
    content_hash: str


def is_syncable_relative_path(rel: str) -> bool:
    if not rel or rel.startswith("digest/"):
        return False
    parts = rel.split("/")
    if len(parts) < 2:
        return False
    return bool(DATE_FOLDER_PATTERN.match(parts[0]))


def _content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_summary_file(root: Path, path: Path) -> Optional[LocalSummaryFile]:
    if path.name.startswith("."):
        return None
    rel = path.relative_to(root).as_posix()
    if not is_syncable_relative_path(rel):
        return None
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    if not has_frontmatter(content):
        return None
    body = strip_leading_frontmatter(content)
    title = extract_title(body, fallback=path.stem)
    return LocalSummaryFile(
        relative_path=rel,
        absolute_path=str(path),
        drive_name=path.name,
        title=title or path.stem,
        content=content,
        content_hash=_content_hash(content),
    )


def normalize_date_folder(value: str) -> str:
    """Accept YYYY_MM_DD or YYYY-MM-DD and return YYYY_MM_DD."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty date folder")
    normalized = raw.replace("-", "_")
    if not DATE_FOLDER_PATTERN.match(normalized):
        raise ValueError(f"invalid date folder: {value!r} (expected YYYY_MM_DD)")
    return normalized


def scan_summary_markdown(
    md_root: str,
    limit: Optional[int] = None,
    date_folder: Optional[str] = None,
) -> List[LocalSummaryFile]:
    root = Path(md_root)
    if not root.is_dir():
        return []

    found: List[LocalSummaryFile] = []

    if date_folder is not None:
        day = root / normalize_date_folder(date_folder)
        if not day.is_dir():
            return []
        for path in sorted(day.glob("*.md")):
            item = _parse_summary_file(root, path)
            if item:
                found.append(item)
        return found

    if limit is not None:
        day_dirs = sorted(
            [p for p in root.iterdir() if p.is_dir() and DATE_FOLDER_PATTERN.match(p.name)],
            reverse=True,
        )
        for day in day_dirs:
            for path in sorted(day.glob("*.md")):
                item = _parse_summary_file(root, path)
                if item:
                    found.append(item)
                    if len(found) >= limit:
                        return found
        return found

    for path in sorted(root.rglob("*.md")):
        item = _parse_summary_file(root, path)
        if item:
            found.append(item)
    return found


def scan_summary_map(
    md_root: str,
    limit: Optional[int] = None,
    date_folder: Optional[str] = None,
) -> Dict[str, LocalSummaryFile]:
    return {
        item.relative_path: item
        for item in scan_summary_markdown(md_root, limit=limit, date_folder=date_folder)
    }
