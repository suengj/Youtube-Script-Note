#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run/worker logging context via contextvars."""

from __future__ import annotations

import contextvars
import logging
from typing import Optional

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")
_worker_id: contextvars.ContextVar[str] = contextvars.ContextVar("worker_id", default="w0")
_video_id: contextvars.ContextVar[str] = contextvars.ContextVar("video_id", default="")
_stage: contextvars.ContextVar[str] = contextvars.ContextVar("stage", default="")
_attempt: contextvars.ContextVar[str] = contextvars.ContextVar("attempt", default="")
_backend: contextvars.ContextVar[str] = contextvars.ContextVar("backend", default="")


def set_pipeline_context(
    *,
    run_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    video_id: Optional[str] = None,
    stage: Optional[str] = None,
    attempt: Optional[str] = None,
    backend: Optional[str] = None,
) -> None:
    if run_id is not None:
        _run_id.set(run_id)
    if worker_id is not None:
        _worker_id.set(worker_id)
    if video_id is not None:
        _video_id.set(video_id)
    if stage is not None:
        _stage.set(stage)
    if attempt is not None:
        _attempt.set(attempt)
    if backend is not None:
        _backend.set(backend)


def clear_video_context() -> None:
    _video_id.set("")
    _stage.set("")
    _attempt.set("")
    _backend.set("")


class PipelineContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get("")
        record.worker_id = _worker_id.get("w0")
        record.video_id = _video_id.get("")
        record.stage = _stage.get("")
        record.attempt = _attempt.get("")
        record.backend = _backend.get("")
        return True


def attach_pipeline_context_filter(logger: Optional[logging.Logger] = None) -> None:
    target = logger or logging.getLogger()
    filt = PipelineContextFilter()
    for handler in target.handlers:
        handler.addFilter(filt)
    target.addFilter(filt)
    fmt = "%(asctime)s - %(name)s - %(levelname)s - run=%(run_id)s worker=%(worker_id)s vid=%(video_id)s stage=%(stage)s - %(message)s"
    for handler in target.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.setFormatter(logging.Formatter(fmt))
