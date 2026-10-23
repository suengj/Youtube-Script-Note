from __future__ import annotations

import sys
from itertools import chain, repeat
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import processed_video_registry as pvr  # noqa: E402


class _Writer:
    def __init__(self, **kwargs):
        self.output_df = kwargs["output_df"]

    def get_output_df(self):
        return self.output_df

    def get_crawl_queue_df(self):
        return None


class _Claims:
    def __init__(self, *_args):
        pass

    def recover_all_stale(self):
        return []


class _InventorySink:
    def __init__(self):
        self.records = []

    def write_inventory(self, record, *, start=False, **_kwargs):
        self.records.append((record, start))
        return {"inventory_revision": len(self.records)}


@pytest.fixture
def budget_run(tmp_path, monkeypatch):
    data, base, md = (tmp_path / name for name in ("data", "base", "md"))
    data.mkdir()
    base.mkdir()
    md.mkdir()
    (data / "input_df.csv").write_text(
        "date,url,category\n2026-10-01,https://youtu.be/abcdefghijk,manual\n"
        "2026-10-01,https://youtu.be/lmnopqrstuv,manual\n",
        encoding="utf-8",
    )
    pvr.write_registry(pd.DataFrame(columns=pvr.REGISTRY_COLUMNS), pvr.registry_path(data))
    sink = _InventorySink()
    cleanup_calls = []
    monkeypatch.setattr(main, "initialize_clients", lambda _config: (None, None, None))
    monkeypatch.setattr(main, "create_transcript_preprocessor", lambda *_args: object())
    monkeypatch.setattr(main, "SharedStateWriter", _Writer)
    monkeypatch.setattr(main, "ClaimManager", _Claims)
    monkeypatch.setattr(main.processed_video_registry, "read_sheet_marked_ids", lambda: set())
    monkeypatch.setattr(main.channel_crawl, "load_channel_df", lambda _root: [])
    monkeypatch.setattr(main, "_run_batch_cleanup", lambda *args, **kwargs: cleanup_calls.append(args))
    monkeypatch.setattr(main, "process_single_video", lambda **_kwargs: pytest.fail("picked a video"))
    monkeypatch.setenv("P03_DRIVE_BACKEND", "api")
    monkeypatch.setenv("P03_DRIVE_SYNC_ENABLED", "0")
    monkeypatch.setenv("P03_RUN_BUDGET_MIN", "1")
    monkeypatch.setenv("P03_MANUAL_SINCE", "2026-10-01")
    config = {
        "BASE_PATH": str(base), "DATA_ROOT": str(data), "WORK_PATH": str(base),
        "HF_HOME": str(tmp_path / "hf"), "OUTPUT_MD_PATH": str(md),
        "CHANNEL_CRAWL": False, "VIDEO_WORKERS": 1, "DRIVE_API_SINK": sink,
        "MIN_WAIT_BETWEEN_VIDEOS": 0, "MAX_WAIT_BETWEEN_VIDEOS": 0,
        "EXTENDED_WAIT_INTERVAL": 100, "EXTENDED_WAIT_DURATION": 0,
        "MAX_CONSECUTIVE_FAILURES": 2, "FAILURE_WAIT_MULTIPLIER": 1,
        "CRAWL_QUEUE_MAX_RETRIES": 2,
    }
    return config, sink, cleanup_calls


def test_over_budget_picks_no_new_video_and_logs_remaining(budget_run, monkeypatch, caplog):
    config, _sink, _cleanup_calls = budget_run
    ticks = chain((0.0,), repeat(61.0))
    monkeypatch.setattr(main.time, "monotonic", lambda: next(ticks))

    main.process_videos(config)

    lines = [record.message for record in caplog.records if record.message.startswith("budget reached:")]
    assert lines == ["budget reached: processed 0, remaining 2"]


def test_budget_stop_runs_end_phase_and_finalizes_inventory(budget_run, monkeypatch):
    config, sink, cleanup_calls = budget_run
    ticks = chain((0.0,), repeat(61.0))
    monkeypatch.setattr(main.time, "monotonic", lambda: next(ticks))

    main.process_videos(config)

    assert cleanup_calls
    assert sink.records[-1][0]["batch_completed"] is True
    assert sink.records[-1][1] is False
