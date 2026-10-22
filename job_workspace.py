#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-video isolated job workspace under tmp/jobs/<video_id>/."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import stat
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

JOBS_SUBDIR = os.path.join("tmp", "jobs")
ACTIVE_JOB_MAX_AGE_SEC = 6 * 3600  # do not delete jobs touched within 6h during cleanup
GENERATION_CACHE_MAX_BYTES = 64 * 1024 * 1024


@dataclass
class VideoJobWorkspace:
    """Isolated filesystem workspace for one video processing job."""

    work_path: str
    video_id: str
    created_at: float = field(default_factory=time.time)

    @property
    def root(self) -> str:
        return os.path.join(self.work_path, JOBS_SUBDIR, self.video_id)

    def ensure(self) -> str:
        os.makedirs(self.root, exist_ok=True)
        return self.root

    @property
    def subs_dir(self) -> str:
        return self.root

    def output_wav_path(self) -> str:
        return os.path.join(self.root, "output.wav")

    def source_audio_path(self, ext: str = "m4a") -> str:
        ext = (ext or "m4a").lstrip(".")
        return os.path.join(self.root, f"source_audio.{ext}")

    def metadata_path(self) -> str:
        return os.path.join(self.root, "job_meta.json")

    def generation_cache_path(self) -> str:
        return os.path.join(self.root, "generation_result.json")

    def load_generation_result(self, *, input_revision: str, model: str, prompt_sha256: str) -> Optional[str]:
        """Load a bounded, revision-bound summary cache; malformed data fails closed."""
        path = self.generation_cache_path()
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > GENERATION_CACHE_MAX_BYTES:
                    return None
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    fd = -1
                    record = json.load(stream)
            finally:
                if fd >= 0:
                    os.close(fd)
            summary = record.get("summary")
            if (record.get("video_id") != self.video_id or
                    record.get("input_revision") != input_revision or
                    record.get("model") != model or
                    record.get("prompt_sha256") != prompt_sha256 or
                    not isinstance(summary, str) or not summary or
                    len(summary.encode("utf-8")) > GENERATION_CACHE_MAX_BYTES or
                    record.get("summary_sha256") != hashlib.sha256(summary.encode("utf-8")).hexdigest()):
                return None
            return summary
        except (OSError, ValueError, TypeError, UnicodeError, AttributeError):
            return None

    def load_generation_model(self, *, input_revision: str, model: str, prompt_sha256: str,
                              default: str = "") -> str:
        """Return the actual final-call model for a valid cached generation record."""
        path = self.generation_cache_path()
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > GENERATION_CACHE_MAX_BYTES:
                    return default
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    fd = -1
                    record = json.load(stream)
            finally:
                if fd >= 0:
                    os.close(fd)
            summary = record.get("summary")
            if (record.get("video_id") != self.video_id or record.get("input_revision") != input_revision or
                    record.get("model") != model or record.get("prompt_sha256") != prompt_sha256 or
                    not isinstance(summary, str) or
                    record.get("summary_sha256") != hashlib.sha256(summary.encode("utf-8")).hexdigest()):
                return default
            return str(record.get("generated_model") or default)
        except (OSError, ValueError, TypeError, UnicodeError, AttributeError):
            return default

    def save_generation_result(self, *, input_revision: str, model: str, prompt_sha256: str, summary: str,
                               generated_model: str = "") -> None:
        """Atomically persist the generated summary and its full binding identity."""
        if not isinstance(summary, str) or not summary or len(summary.encode("utf-8")) > GENERATION_CACHE_MAX_BYTES:
            raise ValueError("invalid generation result")
        self.ensure()
        encoded = summary.encode("utf-8")
        record = {
            "video_id": self.video_id,
            "input_revision": input_revision,
            "model": model,
            "generated_model": generated_model or model,
            "prompt_sha256": prompt_sha256,
            "summary_sha256": hashlib.sha256(encoded).hexdigest(),
            "summary": summary,
        }
        path = self.generation_cache_path()
        tmp = path + f".{os.getpid()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def write_metadata(self, extra: Optional[Dict[str, Any]] = None) -> None:
        self.ensure()
        meta = {
            "video_id": self.video_id,
            "created_at": datetime.utcfromtimestamp(self.created_at).isoformat() + "Z",
            "root": self.root,
        }
        if extra:
            meta.update(extra)
        path = self.metadata_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def touch_active(self) -> None:
        """Mark job as recently active (mtime) so batch cleanup skips it."""
        self.ensure()
        try:
            os.utime(self.root, None)
        except OSError:
            pass

    def list_subtitle_files(self) -> list[str]:
        if not os.path.isdir(self.root):
            return []
        out = []
        for name in os.listdir(self.root):
            low = name.lower()
            if low.endswith(".vtt") or low.endswith(".srt") or name.startswith("subtitle."):
                out.append(os.path.join(self.root, name))
        return out

    def remove_subtitle_files(self) -> int:
        removed = 0
        for path in self.list_subtitle_files():
            try:
                os.remove(path)
                removed += 1
            except OSError as exc:
                logger.debug("Could not remove subtitle %s: %s", path, exc)
        return removed

    def cleanup(self, *, force: bool = False) -> bool:
        """Remove entire job directory. Returns True if removed."""
        if not os.path.isdir(self.root):
            return False
        if not force:
            try:
                age = time.time() - os.path.getmtime(self.root)
                if age < 60:
                    logger.debug("Skipping immediate cleanup for active job %s", self.video_id)
            except OSError:
                pass
        try:
            shutil.rmtree(self.root)
            logger.debug("Removed job workspace %s", self.root)
            return True
        except OSError as exc:
            logger.warning("Failed to remove job workspace %s: %s", self.root, exc)
            return False


def resolve_jobs_root(work_path: str) -> str:
    return os.path.join(work_path, JOBS_SUBDIR)


def cleanup_stale_jobs(
    work_path: str,
    *,
    max_age_sec: int = ACTIVE_JOB_MAX_AGE_SEC,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Remove job directories older than max_age_sec.
    Returns (removed_count, skipped_active_count).
    """
    root = resolve_jobs_root(work_path)
    if not os.path.isdir(root):
        return 0, 0
    now = time.time()
    removed = 0
    skipped = 0
    for name in os.listdir(root):
        job_dir = os.path.join(root, name)
        if not os.path.isdir(job_dir):
            continue
        try:
            mtime = os.path.getmtime(job_dir)
        except OSError:
            continue
        if now - mtime < max_age_sec:
            skipped += 1
            continue
        if dry_run:
            logger.info("[dry-run] Would remove stale job dir: %s", job_dir)
            removed += 1
            continue
        try:
            shutil.rmtree(job_dir)
            removed += 1
        except OSError as exc:
            logger.warning("Failed to remove stale job %s: %s", job_dir, exc)
    return removed, skipped
