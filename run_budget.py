"""Process-wide deadline for paid work started during one P03 run."""

from __future__ import annotations

import threading
import time

try:
    import httpx
except ModuleNotFoundError:  # openai 3.x vendors its compatible client as httpx2
    import httpx2 as httpx

_lock = threading.Lock()
_deadline: float | None = None


class RunBudgetExpired(RuntimeError):
    """Raised before an HTTP attempt when the active P03 run deadline has passed."""


def set_deadline(deadline: float | None) -> None:
    """Set the active run deadline at process entry; values use epoch seconds."""
    global _deadline
    with _lock:
        _deadline = float(deadline) if deadline is not None else None


def expired() -> bool:
    with _lock:
        deadline = _deadline
    return deadline is not None and time.time() >= deadline


class _BudgetExpiredResponse(httpx.Response):
    """Sentinel raised after SDK send handling, so the SDK cannot retry the budget error.

    OpenAI 1.66.3 retries generic exceptions thrown during ``send`` as connection errors.
    Raising here happens after that retry-catching block and escapes as RunBudgetExpired.
    """

    def raise_for_status(self) -> httpx.Response:
        raise RunBudgetExpired("P03 run budget expired before paid LLM HTTP request")


class _BudgetAwareClient(httpx.Client):
    """Check each dispatch while preserving normal HTTPX setup and SDK retry behavior."""

    def send(self, request, *args, **kwargs):
        if expired():
            return _BudgetExpiredResponse(status_code=499, request=request)
        return super().send(request, *args, **kwargs)


def make_http_client(**kwargs) -> httpx.Client:
    """Create an HTTPX client gated before every SDK-managed HTTP attempt."""
    return _BudgetAwareClient(**kwargs)
