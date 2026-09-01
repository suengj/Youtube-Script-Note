#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR2: claim manager, shared state writer, thread-local download errors."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import stt_function_v3 as stt
from claim_manager import ClaimManager
from pipeline_models import VideoProcessResult
from shared_state_writer import SharedStateWriter


@pytest.fixture
def tmp_work(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return str(work)


class TestThreadLocalDownloadError:
    def test_no_cross_thread_pollution(self):
        barrier = threading.Barrier(2)
        results = {}

        def worker(tag, msg):
            stt.clear_last_ytdlp_failure()
            stt._set_last_ytdlp_failure(msg)
            barrier.wait()
            results[tag] = stt.get_last_ytdlp_failure_reason()

        t1 = threading.Thread(target=worker, args=("a", "error-a"))
        t2 = threading.Thread(target=worker, args=("b", "error-b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert results["a"] == "error-a"
        assert results["b"] == "error-b"


class TestClaimManager:
    def test_atomic_claim(self, tmp_work):
        cm = ClaimManager(tmp_work, ttl_sec=3600)
        assert cm.try_claim("vid1", "run1", "w0")
        assert not cm.try_claim("vid1", "run2", "w1")
        cm.release("vid1")
        assert cm.try_claim("vid1", "run2", "w1")

    def test_stale_recovery(self, tmp_work):
        cm = ClaimManager(tmp_work, ttl_sec=1)
        assert cm.try_claim("stale1", "run1", "w0")
        path = cm._path("stale1")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["expires_at"] = "2020-01-01T00:00:00Z"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        assert cm.recover_stale_for_video("stale1")
        assert cm.try_claim("stale1", "run2", "w1")


class TestSharedStateWriter:
    def test_serial_apply_no_lost_update(self, tmp_path):
        data_root = tmp_path / "data"
        data_root.mkdir()
        out_path = str(data_root / "output_df_new.csv")
        df = pd.DataFrame(columns=["date", "url", "v_id", "status"])
        saved_counts = []

        def save_df(frame, path):
            saved_counts.append(len(frame))
            frame.to_csv(path, index=False)

        writer = SharedStateWriter(
            data_root=str(data_root),
            output_df=df,
            output_df_path=out_path,
            save_output_df=save_df,
        )

        def apply_result(vid, st):
            r = VideoProcessResult(
                video_id=vid,
                source_url=f"https://youtube.com/watch?v={vid}",
                status=st,
                worker_id="w0",
            )
            writer.apply(r)

        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = []
            for i in range(20):
                futs.append(ex.submit(apply_result, f"vid{i:03d}", "success"))
            for f in futs:
                f.result()

        final = writer.get_output_df()
        assert len(final) == 20
        assert len(saved_counts) == 20
        assert saved_counts[-1] == 20

    def test_out_of_order_apply(self, tmp_path):
        data_root = tmp_path / "data"
        data_root.mkdir()
        out_path = str(data_root / "output_df_new.csv")
        df = pd.DataFrame(columns=["date", "url", "v_id", "status"])

        writer = SharedStateWriter(
            data_root=str(data_root),
            output_df=df,
            output_df_path=out_path,
            save_output_df=lambda frame, path: frame.to_csv(path, index=False),
        )
        for vid in ("z_last", "a_first"):
            writer.apply(
                VideoProcessResult(
                    video_id=vid,
                    source_url=f"https://youtube.com/watch?v={vid}",
                    status="success",
                )
            )
        assert len(writer.get_output_df()) == 2
        assert set(writer.get_output_df()["v_id"]) == {"z_last", "a_first"}
