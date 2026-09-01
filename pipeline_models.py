#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pipeline dataclasses for worker results and download outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class DownloadResult:
    success: bool
    path: Optional[str] = None
    video_id: Optional[str] = None
    error_reason: Optional[str] = None
    retryable: bool = True


@dataclass
class VideoProcessResult:
    video_id: str
    source_url: str
    status: str
    stage: str = "complete"
    retryable: bool = False
    error_message: Optional[str] = None
    output_md_path: Optional[str] = None
    transcript_source: Optional[str] = None
    transcript_cache_hit: bool = False
    metadata_updates: Dict[str, Any] = field(default_factory=dict)
    queue_updates: Dict[str, Any] = field(default_factory=dict)
    catalog_updates: Dict[str, Any] = field(default_factory=dict)
    prompt_log_entries: list = field(default_factory=list)
    worker_id: str = "w0"
    run_id: str = ""

    @classmethod
    def from_legacy_tuple(
        cls,
        status: str,
        video_id: Optional[str],
        error_msg: Optional[str],
        *,
        source_url: str = "",
        run_id: str = "",
        worker_id: str = "w0",
    ) -> "VideoProcessResult":
        retryable = status in {
            "download_failed",
            "api_error",
            "file_error",
            "mlx_error",
            "error",
        }
        return cls(
            video_id=video_id or "unknown",
            source_url=source_url,
            status=status,
            retryable=retryable,
            error_message=error_msg,
            run_id=run_id,
            worker_id=worker_id,
        )
