#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Process-wide device compute route (Whisper MLX + future on-device LLM)."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_DEVICE_SEM: Optional[threading.Semaphore] = None
_DEVICE_LOCK = threading.Lock()
_DEFAULT_CONCURRENCY = 1


def get_device_semaphore(concurrency: int = _DEFAULT_CONCURRENCY) -> threading.Semaphore:
    global _DEVICE_SEM
    with _DEVICE_LOCK:
        if _DEVICE_SEM is None:
            n = max(1, int(concurrency))
            _DEVICE_SEM = threading.Semaphore(n)
            logger.info("Device route initialized (concurrency=%d)", n)
        return _DEVICE_SEM


@contextmanager
def device_compute_route(*, label: str = "device") -> Iterator[None]:
    """Serialize Whisper MLX and on-device preprocessing LLM on shared GPU/MLX."""
    sem = get_device_semaphore()
    logger.debug("Device route acquire: %s", label)
    sem.acquire()
    try:
        yield
    finally:
        sem.release()
        logger.debug("Device route release: %s", label)


def reset_device_route_for_tests() -> None:
    global _DEVICE_SEM
    with _DEVICE_LOCK:
        _DEVICE_SEM = None
