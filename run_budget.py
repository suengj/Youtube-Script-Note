"""Process-wide deadline for paid work started during one P03 run."""

from __future__ import annotations

import threading
import time
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_lock = threading.Lock()
_deadline: float | None = None

DEFAULT_MAX_RETRIES = 2
_INITIAL_RETRY_DELAY = 0.5
_MAX_RETRY_DELAY = 8.0
_MAX_RETRY_AFTER_DELAY = 60.0


class RunBudgetExpired(RuntimeError):
    """Raised when a paid HTTP attempt is blocked by the active run deadline."""


def set_deadline(deadline: float | None) -> None:
    """Set the active run deadline at process entry; values use epoch seconds."""
    global _deadline
    with _lock:
        _deadline = float(deadline) if deadline is not None else None


def expired() -> bool:
    with _lock:
        deadline = _deadline
    return deadline is not None and time.time() >= deadline


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None

    retry_after_ms = headers.get("retry-after-ms")
    if retry_after_ms:
        try:
            return max(0.0, float(retry_after_ms) / 1000)
        except (TypeError, ValueError):
            pass

    retry_after = headers.get("retry-after")
    if not retry_after:
        return None
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(retry_after)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds)


def _is_retryable_sdk_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    retry_header = headers.get("x-should-retry") if headers is not None else None
    if retry_header == "true":
        return True
    if retry_header == "false":
        return False

    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            if int(status) in (408, 409, 429) or int(status) >= 500:
                return True
        except (TypeError, ValueError):
            pass

    return type(exc).__name__ in {
        "APIConnectionError", "APITimeoutError", "ConnectError", "TimeoutException",
        "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    }


def call_with_retries(call, *args, max_retries: int = DEFAULT_MAX_RETRIES,
                      on_attempt=None, on_error=None, **kwargs):
    """Call a paid SDK operation with SDK-default retries and a deadline check per request."""
    retries = max(0, int(max_retries))
    for retry_index in range(retries + 1):
        if expired():
            raise RunBudgetExpired("P03 run budget expired before paid LLM request")
        if on_attempt is not None:
            on_attempt()
        try:
            return call(*args, **kwargs)
        except Exception as exc:
            if on_error is not None:
                on_error(exc)
            if retry_index >= retries or not _is_retryable_sdk_error(exc):
                raise

            retry_after = _retry_after_seconds(exc)
            if retry_after is not None and 0 < retry_after <= _MAX_RETRY_AFTER_DELAY:
                delay = retry_after
            elif retry_after is not None and retry_after > _MAX_RETRY_AFTER_DELAY:
                raise
            else:
                exponential = min(_INITIAL_RETRY_DELAY * (2 ** retry_index), _MAX_RETRY_DELAY)
                delay = exponential * (1 - 0.25 * random.random())
            time.sleep(delay)
