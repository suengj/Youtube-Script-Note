#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR3: worker pool config, device route, preprocess backend seam."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import get_config_dict, APP_VERSION
from preprocess_backend import CloudApiTranscriptPreprocessor, OnDeviceTranscriptPreprocessor, create_transcript_preprocessor
from runtime_resources import device_compute_route, get_device_semaphore, reset_device_route_for_tests


class TestVersionAndConfig:
    def test_app_version_420(self):
        assert APP_VERSION == "4.2.0"

    def test_video_workers_default(self):
        cfg = get_config_dict()
        assert cfg["VIDEO_WORKERS"] == 2
        assert cfg["DEVICE_COMPUTE_CONCURRENCY"] == 1
        assert cfg["PREPROCESS_BACKEND"] == "cloud_api"


class TestDeviceRoute:
    def setup_method(self):
        reset_device_route_for_tests()

    def teardown_method(self):
        reset_device_route_for_tests()

    def test_concurrency_one(self):
        sem = get_device_semaphore(1)
        acquired = threading.Event()
        release = threading.Event()
        holder = {"ok": False}

        def hold():
            with device_compute_route(label="test"):
                acquired.set()
                release.wait(timeout=5)
            holder["ok"] = True

        t = threading.Thread(target=hold)
        t.start()
        assert acquired.wait(timeout=5)
        blocked = threading.Event()

        def try_second():
            with device_compute_route(label="test2"):
                blocked.set()

        t2 = threading.Thread(target=try_second)
        t2.start()
        time.sleep(0.2)
        assert not blocked.is_set()
        release.set()
        t.join(timeout=5)
        t2.join(timeout=5)
        assert blocked.is_set()
        assert holder["ok"]


class TestPreprocessBackend:
    def test_on_device_disabled(self):
        pre = OnDeviceTranscriptPreprocessor()
        with pytest.raises(NotImplementedError, match="not implemented"):
            pre.minimize("role", "query", "text", model="x")

    def test_factory_cloud(self):
        class FakeClient:
            pass
        pre = create_transcript_preprocessor("cloud_api", FakeClient())
        assert isinstance(pre, CloudApiTranscriptPreprocessor)

    def test_factory_on_device(self):
        pre = create_transcript_preprocessor("on_device", None)
        assert isinstance(pre, OnDeviceTranscriptPreprocessor)
