#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build YT_summary/manifest.yaml inventory."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

import yaml

from scripts.note_catalog_utils import DATE_FOLDER_PATTERN, normalize_date

from .state import SyncStateEntry


def _date_from_relative_path(rel: str) -> Optional[str]:
    parts = rel.split("/")
    if len(parts) < 2:
        return None
    folder = parts[0]
    if not DATE_FOLDER_PATTERN.match(folder):
        return None
    y, mo, d = folder.split("_")
    return f"{y}-{mo}-{d}"


def build_manifest_yaml(
    entries: Dict[str, SyncStateEntry],
    titles_by_rel: Dict[str, str],
    dates_by_rel: Optional[Dict[str, str]] = None,
) -> str:
    items: List[dict] = []
    dates_by_rel = dates_by_rel or {}
    for rel in sorted(entries.keys()):
        entry = entries[rel]
        item: dict = {
            "file": entry.drive_name,
            "title": titles_by_rel.get(rel, entry.drive_name),
        }
        date_val = dates_by_rel.get(rel) or _date_from_relative_path(rel)
        if date_val:
            normalized = normalize_date(date_val)
            if normalized:
                item["date"] = normalized
        items.append(item)
    doc = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "items": items,
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
