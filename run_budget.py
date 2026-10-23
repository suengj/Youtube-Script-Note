"""Process-wide deadline for paid work started during one P03 run."""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_deadline: float | None = None


def set_deadline(deadline: float | None) -> None:
    """Set the active run deadline at process entry; values use epoch seconds."""
    global _deadline
    with _lock:
        _deadline = float(deadline) if deadline is not None else None


def expired() -> bool:
    with _lock:
        deadline = _deadline
    return deadline is not None and time.time() >= deadline
