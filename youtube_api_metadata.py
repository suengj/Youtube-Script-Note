#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube Data API v3 — videos.list batch fetch for title & upload_date.

50개 단위 배치로 조회하여 CPU/네트워크 부하 최소화.
YOUTUBE_API_KEY 필요 (.env).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
BATCH_SIZE = 50
BATCH_DELAY_SEC = 0.3  # API quota 기반, 짧은 딜레이


def _api_request(api_key: str, path: str, params: dict[str, str]) -> Optional[dict[str, Any]]:
    """GET YouTube Data API v3; returns JSON as dict or None on failure."""
    url = f"{YOUTUBE_API_BASE}/{path}?{urlencode({**params, 'key': api_key})}"
    try:
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, OSError, json.JSONDecodeError):
        return None


def _parse_published_at(iso_str: str) -> str:
    """Parse ISO 8601 (2025-01-15T12:00:00Z) to YYYY-MM-DD."""
    if not iso_str:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso_str)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def fetch_video_metadata_batch(
    api_key: str,
    video_ids: list[str],
) -> dict[str, dict[str, str]]:
    """
    Fetch title & upload_date for video IDs via videos.list (batch 50).
    Returns {v_id: {"title": str, "upload_date": "YYYY-MM-DD"}}.
    Quota: 1 unit per 50 ids.
    """
    result: dict[str, dict[str, str]] = {}
    ids = [v for v in video_ids if v and len(v) == 11]
    n_batches = (len(ids) + BATCH_SIZE - 1) // BATCH_SIZE
    iterator = range(0, len(ids), BATCH_SIZE)
    if tqdm:
        iterator = tqdm(iterator, total=n_batches, desc="YouTube API fetch", unit="batch")
    for i in iterator:
        if i > 0:
            time.sleep(BATCH_DELAY_SEC)
        chunk = ids[i : i + BATCH_SIZE]
        data = _api_request(
            api_key,
            "videos",
            {"part": "snippet", "id": ",".join(chunk), "maxResults": str(BATCH_SIZE)},
        )
        if not data:
            continue
        for it in data.get("items", []):
            vid = it.get("id")
            if not vid:
                continue
            vid = str(vid)
            snip = it.get("snippet") or {}
            title = snip.get("title") or ""
            pub = snip.get("publishedAt") or ""
            result[vid] = {
                "title": title,
                "upload_date": _parse_published_at(pub),
            }
        if tqdm and hasattr(iterator, "set_postfix"):
            iterator.set_postfix(fetched=len(result))
    return result


def get_api_key() -> str:
    """Load YOUTUBE_API_KEY from .env."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    return (os.getenv("YOUTUBE_API_KEY") or "").strip()
