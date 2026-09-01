#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Single-run lock for scheduled/manual execution conflict prevention.

Prevents two batch processes from writing the same output/audio/subs concurrently,
which can worsen macOS Errno 11 (EDEADLK) on rename or .part finalization.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
import fcntl
from typing import Optional, Tuple


LOCK_FILENAME = ".p03_speech2text.lock"
LOCK_META_FILENAME = ".p03_speech2text.lock.meta.json"


@dataclass
class RunLockHandle:
    lock_file_path: str
    meta_file_path: str
    fd: Optional[object]

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self.fd.close()
        except Exception:
            pass
        self.fd = None
        try:
            if os.path.exists(self.meta_file_path):
                os.remove(self.meta_file_path)
        except Exception:
            pass


def _read_lock_meta(meta_file_path: str) -> dict:
    if not os.path.exists(meta_file_path):
        return {}
    try:
        with open(meta_file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_lock_meta(meta_file_path: str, meta: dict) -> None:
    with open(meta_file_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _conflict_message(holder: dict, requester_source: str, requester_channel_crawl: bool) -> str:
    holder_source = holder.get("source", "unknown")
    holder_channel_crawl = bool(holder.get("channel_crawl", False))

    # Required explicit messages
    if holder_source == "scheduled" and requester_source == "manual" and not requester_channel_crawl:
        return "cron 작업 실행 중"
    if holder_source == "manual" and not holder_channel_crawl and requester_source == "scheduled":
        return "매뉴얼 df 작업 실행 중"

    # Generic duplicate-run message
    holder_pid = holder.get("pid", "?")
    holder_started_at = holder.get("started_at", "unknown")
    return (
        "another batch is already running "
        f"(pid={holder_pid}, source={holder_source}, started_at={holder_started_at})"
    )


def acquire_run_lock(base_path: str, source: str, channel_crawl: bool) -> Tuple[bool, Optional[RunLockHandle], str]:
    """
    Acquire exclusive process lock.
    Returns (acquired, handle, message).
    """
    os.makedirs(base_path, exist_ok=True)
    lock_file_path = os.path.join(base_path, LOCK_FILENAME)
    meta_file_path = os.path.join(base_path, LOCK_META_FILENAME)

    fd = open(lock_file_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = _read_lock_meta(meta_file_path)
        try:
            fd.close()
        except Exception:
            pass
        return False, None, _conflict_message(holder, source, channel_crawl)

    meta = {
        "pid": os.getpid(),
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,  # scheduled/manual
        "channel_crawl": bool(channel_crawl),
    }
    _write_lock_meta(meta_file_path, meta)

    return True, RunLockHandle(lock_file_path=lock_file_path, meta_file_path=meta_file_path, fd=fd), ""
