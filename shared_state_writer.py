#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Main-thread serial writer for shared CSV/JSONL/catalog state."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Any, Callable, Optional

import pandas as pd

import channel_crawl
from pipeline_models import VideoProcessResult

logger = logging.getLogger(__name__)


class SharedStateWriter:
    """Apply VideoProcessResult updates serially on the main thread."""

    def __init__(
        self,
        *,
        data_root: str,
        output_df: pd.DataFrame,
        output_df_path: str,
        save_output_df: Callable[[pd.DataFrame, str], None],
        crawl_queue_df: Optional[pd.DataFrame] = None,
        save_crawl_queue: Optional[Callable[[Any, str], None]] = None,
        channel_crawl_enabled: bool = False,
        queue_url_to_video_id: Optional[dict] = None,
        append_metadata_jsonl: Optional[Callable[..., None]] = None,
        append_catalog: Optional[Callable[..., None]] = None,
        append_prompt_log: Optional[Callable[..., None]] = None,
        prompt_log_path: str = "",
    ):
        self.data_root = data_root
        self.output_df = output_df
        self.output_df_path = output_df_path
        self.save_output_df = save_output_df
        self.crawl_queue_df = crawl_queue_df
        self.save_crawl_queue = save_crawl_queue
        self.channel_crawl_enabled = channel_crawl_enabled
        self.queue_url_to_video_id = queue_url_to_video_id or {}
        self.append_metadata_jsonl = append_metadata_jsonl
        self.append_catalog = append_catalog
        self.append_prompt_log = append_prompt_log
        self.prompt_log_path = prompt_log_path
        self._lock = threading.Lock()

    def apply(self, result: VideoProcessResult) -> None:
        with self._lock:
            self._apply_unlocked(result)

    def _apply_unlocked(self, result: VideoProcessResult) -> None:
        v_date = datetime.today().strftime("%Y-%m-%d")
        new_row = {
            "date": v_date,
            "url": result.source_url,
            "v_id": result.video_id if result.video_id else "unknown",
            "status": result.status,
        }
        self.output_df = pd.concat(
            [self.output_df, pd.DataFrame([new_row])],
            ignore_index=True,
        )
        self.save_output_df(self.output_df, self.output_df_path)
        logger.info(
            "[TRACK] output_df append: status=%s, v_id=%s, url=%s worker=%s",
            result.status,
            new_row["v_id"],
            result.source_url,
            result.worker_id,
        )

        if self.channel_crawl_enabled and self.crawl_queue_df is not None and self.save_crawl_queue:
            queue_video_id = (result.video_id or "").strip()
            if not queue_video_id:
                queue_video_id = self.queue_url_to_video_id.get(result.source_url, "")
            self.crawl_queue_df = channel_crawl.apply_result_to_queue(
                self.crawl_queue_df,
                queue_video_id,
                result.status,
                result.error_message,
            )
            self.save_crawl_queue(self.data_root, self.crawl_queue_df)
            logger.info(
                "[TRACK] crawl_queue update: status=%s, v_id=%s, url=%s",
                result.status,
                queue_video_id or "unknown",
                result.source_url,
            )

        meta = result.metadata_updates or {}
        if meta.get("jsonl") and self.append_metadata_jsonl:
            j = meta["jsonl"]
            try:
                self.append_metadata_jsonl(
                    j.get("path", ""),
                    j.get("upload_date", ""),
                    j.get("video_id", result.video_id),
                    j.get("transcript_date", v_date),
                    j.get("method", ""),
                    j.get("md_path", ""),
                    has_yid=j.get("has_yid", True),
                )
            except Exception as exc:
                logger.warning("metadata jsonl append failed: %s", exc)

        if result.catalog_updates and self.append_catalog:
            try:
                self.append_catalog(
                    result.catalog_updates.get("work_path", ""),
                    result.catalog_updates.get("data_root", self.data_root),
                    result.catalog_updates.get("entry", {}),
                )
            except Exception as exc:
                logger.warning("catalog append failed: %s", exc)

        for entry in result.prompt_log_entries or []:
            if self.append_prompt_log:
                try:
                    self.append_prompt_log(
                        v_prompt=entry.get("prompt", ""),
                        v_task=entry.get("task", ""),
                        log_path=self.prompt_log_path,
                    )
                except Exception as exc:
                    logger.warning("prompt log append failed: %s", exc)

    def get_output_df(self) -> pd.DataFrame:
        return self.output_df

    def get_crawl_queue_df(self):
        return self.crawl_queue_df
