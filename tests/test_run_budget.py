from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.mark.parametrize("value", [None, "", "bad", "0", "-2", "nan", "inf"])
def test_run_budget_invalid_values_use_100_minute_default(monkeypatch, value):
    import main

    if value is None:
        monkeypatch.delenv("P03_RUN_BUDGET_MIN", raising=False)
        assert main._run_budget_seconds() == 6000
    else:
        monkeypatch.setenv("P03_RUN_BUDGET_MIN", value)
        assert main._run_budget_seconds() == 6000


def test_run_budget_stops_queue_and_finalizes_inventory(tmp_path, monkeypatch, caplog):
    import main
    import processed_video_registry as registry
    from pipeline_models import VideoProcessResult

    data, base, notes = (tmp_path / name for name in ("data", "base", "notes"))
    data.mkdir(); base.mkdir(); notes.mkdir()
    registry.write_registry(pd.DataFrame(columns=registry.REGISTRY_COLUMNS), registry.registry_path(data))
    ids = ["abcdefghijk", "lmnopqrstuv", "wxyz0123456"]
    (data / "input_df.csv").write_text(
        "date,url,category\n" + "".join(
            f"2026-10-22,https://youtu.be/{video_id},manual\n" for video_id in ids
        ), encoding="cp949",
    )

    now = [0.0]
    monkeypatch.setattr(main.time, "time", lambda: now[0])
    monkeypatch.setenv("P03_RUN_BUDGET_MIN", "1")
    monkeypatch.setenv("P03_DRIVE_BACKEND", "api")
    monkeypatch.setenv("P03_DRIVE_SYNC_ENABLED", "0")
    monkeypatch.setenv("P03_MANUAL_SINCE", "2026-10-01")
    monkeypatch.delenv("P03_SHEET_IMPORT_ENABLED", raising=False)
    monkeypatch.setattr(main, "initialize_clients", lambda _config: (None, None, None))
    monkeypatch.setattr(main.processed_video_registry, "read_sheet_marked_ids", lambda: set())
    monkeypatch.setattr(main.channel_crawl, "load_channel_df", lambda _root: [])
    monkeypatch.setattr(main, "create_transcript_preprocessor", lambda *_args: object())
    monkeypatch.setattr(main, "_run_batch_cleanup", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(main, "load_config", lambda: config)
    monkeypatch.setattr(main.run_lock, "acquire_run_lock", lambda *_args: (True, None, ""))
    monkeypatch.setenv("P03_STATE_BACKUP", "1")
    backup_calls = []
    monkeypatch.setattr("p03_processed_video_backup.backup_processed_registry",
                        lambda *_args, **_kwargs: backup_calls.append("processed"))
    monkeypatch.setattr(main, "backup_state", lambda *_args, **_kwargs: backup_calls.append("state") or "ok")
    monkeypatch.setattr("drive_api_sink.DriveApiSink", lambda: object())

    class Writer:
        def __init__(self, **kwargs): self.df = kwargs["output_df"]
        def apply(self, result):
            self.df.loc[len(self.df)] = {
                "date": "2026-10-23", "url": result.source_url,
                "v_id": result.video_id, "status": result.status,
            }
            main._save_output_df_with_retry(self.df, str(data / "output_df_new.csv"), na_rep="")
        def get_output_df(self): return self.df
        def get_crawl_queue_df(self): return None

    class Claims:
        claimed = []
        def __init__(self, *_args): pass
        def recover_all_stale(self): return []
        def try_claim(self, video_id, *_args, **_kwargs):
            self.claimed.append(video_id)
            return True
        def release(self, *_args): pass

    class Inventory:
        def __init__(self): self.records = []
        def write_inventory(self, record, **_kwargs):
            self.records.append(record)
            return {**record, "inventory_revision": len(self.records)}

    seen = []
    def process_one(**kwargs):
        video_id = kwargs["v_url"].split("v=", 1)[-1]
        seen.append(video_id)
        now[0] = 61.0
        return VideoProcessResult(video_id=video_id, source_url=kwargs["v_url"],
                                  status="download_failed", retryable=True,
                                  error_message="temporary failure")

    monkeypatch.setattr(main, "SharedStateWriter", Writer)
    monkeypatch.setattr(main, "ClaimManager", Claims)
    monkeypatch.setattr(main, "process_single_video", process_one)
    sink = Inventory()
    config = {
        "BASE_PATH": str(base), "DATA_ROOT": str(data), "WORK_PATH": str(base),
        "HF_HOME": str(tmp_path / "hf"), "OUTPUT_MD_PATH": str(notes),
        "CHANNEL_CRAWL": False, "VIDEO_WORKERS": 1, "DRIVE_API_SINK": sink,
        "MIN_WAIT_BETWEEN_VIDEOS": 0, "MAX_WAIT_BETWEEN_VIDEOS": 0,
        "EXTENDED_WAIT_INTERVAL": 100, "EXTENDED_WAIT_DURATION": 0,
        "MAX_CONSECUTIVE_FAILURES": 2, "FAILURE_WAIT_MULTIPLIER": 1,
        "CRAWL_QUEUE_MAX_RETRIES": 2, "_RUN_START_TIME": 0,
    }

    main.main([])

    assert seen == [ids[0]]
    assert Claims.claimed == [ids[0]]
    final_inventory = sink.records[-1]
    assert final_inventory["source_counts"]["unresolved"] >= 2
    assert final_inventory["inventory_complete"] is False
    assert "run_budget_reached" in final_inventory["incomplete_reason"]
    assert "P03_RUN_BUDGET_REACHED elapsed=61.0 remaining=2" in caplog.text
    assert backup_calls == ["processed", "state"]


def test_run_budget_not_hit_keeps_processing_all_videos(tmp_path, monkeypatch):
    import main
    import processed_video_registry as registry
    from pipeline_models import VideoProcessResult

    data, base, notes = (tmp_path / name for name in ("data", "base", "notes"))
    data.mkdir(); base.mkdir(); notes.mkdir()
    registry.write_registry(pd.DataFrame(columns=registry.REGISTRY_COLUMNS), registry.registry_path(data))
    ids = ["abcdefghijk", "lmnopqrstuv"]
    (data / "input_df.csv").write_text(
        "date,url,category\n" + "".join(
            f"2026-10-22,https://youtu.be/{video_id},manual\n" for video_id in ids
        ), encoding="cp949",
    )
    monkeypatch.setenv("P03_RUN_BUDGET_MIN", "100")
    monkeypatch.setenv("P03_DRIVE_BACKEND", "desktop")
    monkeypatch.setenv("P03_DRIVE_SYNC_ENABLED", "0")
    monkeypatch.setenv("P03_MANUAL_SINCE", "2026-10-01")
    monkeypatch.delenv("P03_SHEET_IMPORT_ENABLED", raising=False)
    monkeypatch.setattr(main.time, "time", lambda: 10.0)
    monkeypatch.setattr(main, "initialize_clients", lambda _config: (None, None, None))
    monkeypatch.setattr(main.processed_video_registry, "read_sheet_marked_ids", lambda: set())
    monkeypatch.setattr(main.channel_crawl, "load_channel_df", lambda _root: [])
    monkeypatch.setattr(main, "create_transcript_preprocessor", lambda *_args: object())
    monkeypatch.setattr(main, "_run_batch_cleanup", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main.time, "sleep", lambda *_args: None)

    class Writer:
        def __init__(self, **kwargs): self.df = kwargs["output_df"]
        def apply(self, result):
            self.df.loc[len(self.df)] = {"date": "2026-10-23", "url": result.source_url,
                                         "v_id": result.video_id, "status": result.status}
        def get_output_df(self): return self.df
        def get_crawl_queue_df(self): return None

    class Claims:
        def __init__(self, *_args): pass
        def recover_all_stale(self): return []
        def try_claim(self, *_args, **_kwargs): return True
        def release(self, *_args): pass

    seen = []
    def process_one(**kwargs):
        video_id = kwargs["v_url"].split("v=", 1)[-1]
        seen.append(video_id)
        return VideoProcessResult(video_id=video_id, source_url=kwargs["v_url"], status="video_unavailable")

    monkeypatch.setattr(main, "SharedStateWriter", Writer)
    monkeypatch.setattr(main, "ClaimManager", Claims)
    monkeypatch.setattr(main, "process_single_video", process_one)
    config = {
        "BASE_PATH": str(base), "DATA_ROOT": str(data), "WORK_PATH": str(base),
        "HF_HOME": str(tmp_path / "hf"), "OUTPUT_MD_PATH": str(notes),
        "CHANNEL_CRAWL": False, "VIDEO_WORKERS": 1,
        "MIN_WAIT_BETWEEN_VIDEOS": 0, "MAX_WAIT_BETWEEN_VIDEOS": 0,
        "EXTENDED_WAIT_INTERVAL": 100, "EXTENDED_WAIT_DURATION": 0,
        "MAX_CONSECUTIVE_FAILURES": 2, "FAILURE_WAIT_MULTIPLIER": 1,
        "CRAWL_QUEUE_MAX_RETRIES": 2, "_RUN_START_TIME": main.time.time(),
    }
    main.process_videos(config)
    assert seen == ids


def test_successful_manual_ledger_row_survives_next_dispatch(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    import main
    import processed_video_registry as registry
    from pipeline_models import VideoProcessResult
    from scripts.drive_yt_summary import publish, sync

    data, base, notes = (tmp_path / name for name in ("data", "base", "notes"))
    data.mkdir(); base.mkdir(); notes.mkdir()
    registry.write_registry(pd.DataFrame(columns=registry.REGISTRY_COLUMNS), registry.registry_path(data))
    ids = ["abcdefghijk", "lmnopqrstuv"]
    (data / "input_df.csv").write_text(
        "date,url,category\n" + "".join(
            f"2026-10-22,https://youtu.be/{video_id},manual\n" for video_id in ids
        ), encoding="cp949",
    )
    monkeypatch.setenv("P03_RUN_BUDGET_MIN", "100")
    monkeypatch.setenv("P03_DRIVE_BACKEND", "desktop")
    monkeypatch.setenv("P03_DRIVE_SYNC_ENABLED", "0")
    monkeypatch.setenv("P03_MANUAL_SINCE", "2026-10-01")
    monkeypatch.delenv("P03_SHEET_IMPORT_ENABLED", raising=False)
    monkeypatch.setattr(main.time, "time", lambda: 10.0)
    monkeypatch.setattr(main, "initialize_clients", lambda _config: (object(), None, object()))
    monkeypatch.setattr(main.processed_video_registry, "read_sheet_marked_ids", lambda: set())
    monkeypatch.setattr(main.channel_crawl, "load_channel_df", lambda _root: [])
    monkeypatch.setattr(main, "create_transcript_preprocessor", lambda *_args: object())
    monkeypatch.setattr(main, "_run_batch_cleanup", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(publish, "flush_staging", lambda **_kwargs: [])
    monkeypatch.setattr(sync, "run_sync_safe", lambda **_kwargs: SimpleNamespace(
        errors=0, created=0, updated=0, skipped=0, error_messages=[]))

    class Writer:
        def __init__(self, **kwargs): self.df = kwargs["output_df"]
        def apply(self, result):
            self.df.loc[len(self.df)] = {"date": "2026-10-23", "url": result.source_url,
                                         "v_id": result.video_id, "status": result.status}
        def get_output_df(self): return self.df
        def get_crawl_queue_df(self): return None

    class Claims:
        def __init__(self, *_args): pass
        def recover_all_stale(self): return []
        def try_claim(self, *_args, **_kwargs): return True
        def release(self, *_args): pass

    md_path = notes / "first.md"
    md_text = "summary for https://youtube.com/watch?v=abcdefghijk\n"
    md_path.write_text(md_text, encoding="utf-8")
    seen = []

    def process_one(**kwargs):
        video_id = kwargs["v_url"].split("v=", 1)[-1]
        seen.append(video_id)
        if video_id == ids[0]:
            return VideoProcessResult(
                video_id=video_id, source_url=kwargs["v_url"], status="success",
                output_md_path=str(md_path), metadata_updates={"drive_verified": True, "model": "test-model"},
            )
        return VideoProcessResult(video_id=video_id, source_url=kwargs["v_url"], status="video_unavailable")

    monkeypatch.setattr(main, "SharedStateWriter", Writer)
    monkeypatch.setattr(main, "ClaimManager", Claims)
    monkeypatch.setattr(main, "process_single_video", process_one)
    config = {
        "BASE_PATH": str(base), "DATA_ROOT": str(data), "WORK_PATH": str(base),
        "HF_HOME": str(tmp_path / "hf"), "OUTPUT_MD_PATH": str(notes),
        "CHANNEL_CRAWL": False, "VIDEO_WORKERS": 1,
        "MIN_WAIT_BETWEEN_VIDEOS": 0, "MAX_WAIT_BETWEEN_VIDEOS": 0,
        "EXTENDED_WAIT_INTERVAL": 100, "EXTENDED_WAIT_DURATION": 0,
        "MAX_CONSECUTIVE_FAILURES": 2, "FAILURE_WAIT_MULTIPLIER": 1,
        "CRAWL_QUEUE_MAX_RETRIES": 2, "_RUN_START_TIME": 10.0,
    }

    main.process_videos(config)

    ledger = main.manual_video_input.read_ledger(data / "manual_input_status.csv")
    row = ledger.loc[ledger.video_id == ids[0]].iloc[0]
    assert seen == ids
    assert row.status == "success"
    assert row.md_sha256 == hashlib.sha256(md_text.encode("utf-8")).hexdigest()
