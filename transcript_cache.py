#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plain transcript cache with TTL and atomic writes."""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PARSER_VERSION = "1"
CACHE_SCHEMA_VERSION = 1


def _utc_iso(ts: float | None = None) -> str:
    t = ts if ts is not None else time.time()
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def source_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


class TranscriptCache:
    def __init__(
        self,
        cache_root: str,
        *,
        enabled: bool = True,
        ttl_hours: int = 72,
    ):
        self.cache_root = cache_root
        self.enabled = enabled
        self.ttl_hours = ttl_hours
        self.transcripts_dir = os.path.join(cache_root, "transcripts")

    def _path(self, video_id: str) -> str:
        safe = (video_id or "unknown").replace("/", "_")
        return os.path.join(self.transcripts_dir, f"{safe}.json")

    def get(self, video_id: str) -> Optional[Dict[str, Any]]:
        if not self.enabled or not video_id:
            return None
        path = self._path(video_id)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Invalid transcript cache %s: %s", path, exc)
            return None
        expires_at = entry.get("expires_at") or ""
        try:
            exp_ts = datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            if time.time() > exp_ts:
                return None
        except (ValueError, TypeError):
            return None
        text = (entry.get("text") or "").strip()
        if not text:
            return None
        return entry

    def get_text(self, video_id: str) -> Optional[str]:
        entry = self.get(video_id)
        return entry.get("text") if entry else None

    def put(
        self,
        video_id: str,
        text: str,
        *,
        transcript_source: str,
        content_hash: str = "",
    ) -> str:
        if not self.enabled:
            return ""
        if not video_id or not (text or "").strip():
            raise ValueError("video_id and non-empty text required for cache put")
        os.makedirs(self.transcripts_dir, exist_ok=True)
        now = time.time()
        expires = now + self.ttl_hours * 3600
        entry = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "video_id": video_id,
            "source_hash": content_hash or source_hash(text),
            "parser_version": PARSER_VERSION,
            "transcript_source": transcript_source,
            "created_at": _utc_iso(now),
            "expires_at": _utc_iso(expires),
            "text": text,
        }
        path = self._path(video_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return path

    def cleanup_expired(self, *, dry_run: bool = False) -> int:
        if not os.path.isdir(self.transcripts_dir):
            return 0
        removed = 0
        now = time.time()
        for path in glob.glob(os.path.join(self.transcripts_dir, "*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                expires_at = entry.get("expires_at") or ""
                exp_ts = datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                ).timestamp()
                if now <= exp_ts:
                    continue
            except (OSError, json.JSONDecodeError, ValueError):
                pass
            if dry_run:
                logger.info("[dry-run] Would remove expired cache %s", path)
            else:
                try:
                    os.remove(path)
                except OSError:
                    continue
            removed += 1
        return removed


def find_durable_full_transcript(output_full_path: str, video_id: str) -> Optional[str]:
    """Return path to existing durable full transcript containing video_id, if any."""
    if not output_full_path or not video_id or not os.path.isdir(output_full_path):
        return None
    patterns = [
        os.path.join(output_full_path, f"*{video_id}*.txt"),
        os.path.join(output_full_path, f"*vid-{video_id}*.txt"),
        os.path.join(output_full_path, f"*+vid-{video_id}.txt"),
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        for path in matches:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
    return None


def should_write_transcript_cache(
    *,
    enabled: bool,
    durable_full_path: Optional[str],
    subs_source: Optional[str],
    save_full_when_auto_subs: bool,
) -> bool:
    """Cache when no durable full will exist (e.g. auto_subs without SAVE_FULL)."""
    if not enabled:
        return False
    if durable_full_path and os.path.isfile(durable_full_path):
        return False
    if subs_source == "auto" and not save_full_when_auto_subs:
        return True
    # Whisper always writes durable full; cache optional for resume before API steps
    if subs_source in (None, "") and not durable_full_path:
        return True
    return False
