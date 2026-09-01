#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Process-level YouTube download admission limiter (shared across workers)."""

from __future__ import annotations

import random
import threading
import time
from typing import Optional


class DownloadAdmissionLimiter:
    """
    Ensures new YouTube download starts respect MIN/MAX inter-video spacing
    without forcing in-flight workers to sleep.
    """

    def __init__(self, min_wait: float, max_wait: float):
        self.min_wait = min_wait
        self.max_wait = max_wait
        self._lock = threading.Lock()
        self._last_admit: float = 0.0
        self._extended_block_until: float = 0.0
        self._consecutive_failures: int = 0

    def wait_for_admission(self) -> None:
        with self._lock:
            now = time.time()
            if now < self._extended_block_until:
                sleep_s = self._extended_block_until - now
            else:
                gap = random.uniform(self.min_wait, self.max_wait)
                elapsed = now - self._last_admit
                sleep_s = max(0.0, gap - elapsed)
            if sleep_s > 0:
                time.sleep(sleep_s)
            self._last_admit = time.time()

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self, *, extended_duration: float = 0.0, max_consecutive: int = 10) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= max_consecutive and extended_duration > 0:
                self._extended_block_until = time.time() + extended_duration
                self._consecutive_failures = 0

    def set_extended_block(self, duration_sec: float) -> None:
        with self._lock:
            self._extended_block_until = time.time() + duration_sec


class ProviderCooldown:
    """Shared cooldown after LLM provider 429/5xx."""

    def __init__(self, base_wait: float = 30.0):
        self.base_wait = base_wait
        self._lock = threading.Lock()
        self._cooldown_until: float = 0.0

    def note_rate_limit(self, multiplier: float = 2.0) -> None:
        with self._lock:
            self._cooldown_until = max(
                self._cooldown_until,
                time.time() + self.base_wait * multiplier,
            )

    def wait_if_needed(self) -> None:
        with self._lock:
            now = time.time()
            if now < self._cooldown_until:
                time.sleep(self._cooldown_until - now)
