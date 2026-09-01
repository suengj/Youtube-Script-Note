#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transcript preprocessing backend seam (cloud API vs on-device stub)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

from runtime_resources import device_compute_route

logger = logging.getLogger(__name__)


class TranscriptPreprocessor(ABC):
    @abstractmethod
    def minimize(
        self,
        role: str,
        query: str,
        transcription: str,
        *,
        model: str,
        token_limit: Optional[int] = None,
        skip_merge_reminimize: bool = False,
    ) -> str:
        ...


class CloudApiTranscriptPreprocessor(TranscriptPreprocessor):
    """Existing OpenAI GPT Nano path via token_minimizer_chunked."""

    def __init__(self, client: Any):
        self.client = client

    def minimize(
        self,
        role: str,
        query: str,
        transcription: str,
        *,
        model: str,
        token_limit: Optional[int] = None,
        skip_merge_reminimize: bool = False,
    ) -> str:
        import stt_function_v3 as stt

        return stt.token_minimizer_chunked(
            role,
            query,
            transcription,
            self.client,
            model=model,
            token_limit=token_limit,
            skip_merge_reminimize=skip_merge_reminimize,
        )


class OnDeviceTranscriptPreprocessor(TranscriptPreprocessor):
    """Reserved stub — not enabled in v4.2."""

    def minimize(
        self,
        role: str,
        query: str,
        transcription: str,
        *,
        model: str,
        token_limit: Optional[int] = None,
        skip_merge_reminimize: bool = False,
    ) -> str:
        with device_compute_route(label="on_device_preprocess"):
            raise NotImplementedError(
                "PREPROCESS_BACKEND=on_device is reserved but not implemented in v4.2. "
                "Use PREPROCESS_BACKEND=cloud_api."
            )


def create_transcript_preprocessor(backend: str, client: Any) -> TranscriptPreprocessor:
    b = (backend or "cloud_api").strip().lower()
    if b in ("cloud_api", "cloud", "openai"):
        return CloudApiTranscriptPreprocessor(client)
    if b in ("on_device", "on-device", "local"):
        return OnDeviceTranscriptPreprocessor()
    raise ValueError(f"Unknown PREPROCESS_BACKEND: {backend}")
