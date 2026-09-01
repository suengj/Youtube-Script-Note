#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local idempotent sync state for YT_summary filesystem sync."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict


STATE_VERSION = 2


@dataclass
class SyncStateEntry:
    relative_path: str
    content_hash: str
    dest_path: str
    drive_name: str
    updated_at: str

    @property
    def drive_file_id(self) -> str:
        """Backward-compat alias used by older state/manifest code."""
        return self.dest_path


@dataclass
class SyncState:
    files: Dict[str, SyncStateEntry]
    manifest_path: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_state(path: Path) -> SyncState:
    if not path.is_file():
        return SyncState(files={})
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SyncState(files={})

    manifest_path = ""
    if isinstance(raw, dict):
        manifest_path = (raw.get("manifest_path") or raw.get("manifest_drive_file_id") or "").strip()

    files_raw = raw.get("files") if isinstance(raw, dict) else {}
    out: Dict[str, SyncStateEntry] = {}
    for rel, entry in (files_raw or {}).items():
        if not isinstance(entry, dict):
            continue
        dest = (entry.get("dest_path") or entry.get("drive_file_id") or "").strip()
        if not dest:
            continue
        out[rel] = SyncStateEntry(
            relative_path=rel,
            content_hash=(entry.get("content_hash") or "").strip(),
            dest_path=dest,
            drive_name=(entry.get("drive_name") or "").strip(),
            updated_at=(entry.get("updated_at") or "").strip(),
        )
    return SyncState(files=out, manifest_path=manifest_path)


def save_state(path: Path, state: SyncState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "updated_at": _now_iso(),
        "manifest_path": state.manifest_path,
        "files": {
            rel: {
                "relative_path": e.relative_path,
                "content_hash": e.content_hash,
                "dest_path": e.dest_path,
                "drive_name": e.drive_name,
                "updated_at": e.updated_at,
            }
            for rel, e in sorted(state.files.items())
        },
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
