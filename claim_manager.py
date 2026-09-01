#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Atomic per-video claim markers for duplicate processing prevention."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)

CLAIMS_SUBDIR = os.path.join("tmp", "claims")
DEFAULT_CLAIM_TTL_SEC = 3600


@dataclass
class ClaimRecord:
    video_id: str
    run_id: str
    worker_id: str
    claimed_at: str
    expires_at: str
    source_queue: str = "queue"


class ClaimManager:
    def __init__(self, work_path: str, ttl_sec: int = DEFAULT_CLAIM_TTL_SEC):
        self.claims_dir = os.path.join(work_path, CLAIMS_SUBDIR)
        self.ttl_sec = ttl_sec
        os.makedirs(self.claims_dir, exist_ok=True)

    def _path(self, video_id: str) -> str:
        safe = (video_id or "unknown").replace("/", "_")
        return os.path.join(self.claims_dir, f"{safe}.json")

    def _utc_iso(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def read_claim(self, video_id: str) -> Optional[ClaimRecord]:
        path = self._path(video_id)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return ClaimRecord(**data)
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def is_claim_valid(self, video_id: str, *, run_id: Optional[str] = None) -> bool:
        rec = self.read_claim(video_id)
        if not rec:
            return False
        try:
            exp = datetime.strptime(rec.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            if time.time() > exp:
                return False
        except ValueError:
            return False
        if run_id and rec.run_id != run_id:
            return True
        return True

    def try_claim(
        self,
        video_id: str,
        run_id: str,
        worker_id: str,
        *,
        source_queue: str = "queue",
    ) -> bool:
        if not video_id:
            return False
        self.recover_stale_for_video(video_id)
        existing = self.read_claim(video_id)
        if existing and self.is_claim_valid(video_id) and existing.run_id != run_id:
            logger.debug("Claim denied for %s (held by %s)", video_id, existing.worker_id)
            return False
        path = self._path(video_id)
        now = time.time()
        rec = ClaimRecord(
            video_id=video_id,
            run_id=run_id,
            worker_id=worker_id,
            claimed_at=self._utc_iso(now),
            expires_at=self._utc_iso(now + self.ttl_sec),
            source_queue=source_queue,
        )
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rec.__dict__, f, ensure_ascii=False, indent=2)
            return True
        except FileExistsError:
            return False
        except OSError as exc:
            logger.warning("Claim failed for %s: %s", video_id, exc)
            return False

    def release(self, video_id: str) -> None:
        path = self._path(video_id)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            logger.debug("Release claim %s: %s", video_id, exc)

    def recover_stale_for_video(self, video_id: str) -> bool:
        rec = self.read_claim(video_id)
        if not rec:
            return False
        try:
            exp = datetime.strptime(rec.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            if time.time() <= exp:
                return False
        except ValueError:
            pass
        self.release(video_id)
        logger.info("Recovered stale claim for %s", video_id)
        return True

    def recover_all_stale(self) -> List[str]:
        recovered = []
        if not os.path.isdir(self.claims_dir):
            return recovered
        for name in os.listdir(self.claims_dir):
            if not name.endswith(".json"):
                continue
            vid = name[:-5]
            if self.recover_stale_for_video(vid):
                recovered.append(vid)
        return recovered
