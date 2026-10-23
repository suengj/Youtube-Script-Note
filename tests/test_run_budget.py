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


def test_expired_run_budget_blocks_paid_work_from_all_recovery_paths(tmp_path, monkeypatch):
    import hashlib
    import time
    from types import SimpleNamespace

    import manual_video_input as mvi
    import run_budget
    import stt_function_v3 as stt
    from llm_responses import call_responses, ResponsesCallError
    from scripts.drive_yt_summary import publish, sync
    from scripts.drive_yt_summary.publish import PublishResult
    from scripts.jev_classify import classify as jev_classify

    paid_calls = []
    monkeypatch.setattr(jev_classify, "classify_after_publish", lambda *_: paid_calls.append("publish"))

    # Prepare an error entry before the run deadline expires so retry_errors has work queued.
    source_dir = tmp_path / "YT_summary" / "source"
    source_dir.mkdir(parents=True)
    source = source_dir / "failed.md"
    video_id = "p8rphwT9g-A"
    source.write_text(f"---\nvid: {video_id}\nsource_url: https://youtu.be/{video_id}\n---\nbody\n",
                      encoding="utf-8")
    index_path = source_dir.parent / "classification-index.json"
    class Provider:
        name, model, prompt_version = "jev", "jev-test", "test"
        calls = 0
        def classify_detail(self, _text):
            self.calls += 1
            raise RuntimeError("prepare retry entry")
    provider = Provider()
    monkeypatch.setattr(run_budget, "_deadline", None)
    jev_classify.classify_if_needed(source, index_path, provider=provider)
    provider.calls = 0
    monkeypatch.setattr(run_budget, "_deadline", time.time() - 1)
    monkeypatch.setenv("P03_JEV_CLASSIFY_ENABLED", "1")

    # Pending Drive recovery: the ordinary publish and forced rewrite both reach the
    # post-write classification sink, while their storage/readback path still runs.
    staged_root = tmp_path / "work" / "output_md_staging" / "2026"
    staged_root.mkdir(parents=True)
    staged = staged_root / "manual.md"
    content = "already generated summary"
    staged.write_text(content, encoding="utf-8")
    destination = tmp_path / "drive" / "manual.md"
    destination.parent.mkdir()
    destination.write_text("stale", encoding="utf-8")
    ledger = mvi.upsert_ledger(mvi.read_ledger(tmp_path / "missing.csv"), video_id="abcdefghijk",
        status="retryable_failed", reason="pending_drive: prior run", md_path=str(staged),
        md_sha256=hashlib.sha256(content.encode()).hexdigest())
    publish_calls = []

    def recovery_publisher(**kwargs):
        publish_calls.append(kwargs)
        if kwargs.get("force_rewrite"):
            destination.write_text(kwargs["content"], encoding="utf-8")
        result = PublishResult(drive_action="unchanged", drive_path=str(destination))
        publish._classify_after_drive_write(result, destination.parent)
        return result

    mvi.retry_pending_drive(ledger, base_path=str(tmp_path), work_path=str(tmp_path / "work"),
                            md_root=str(tmp_path / "md"), publisher=recovery_publisher)
    assert len(publish_calls) == 2 and publish_calls[1].get("force_rewrite") is True

    # Staging flush uses the same sink after its publish callback.
    staged_flush = staged_root / "flush.md"
    staged_flush.write_text("flushed summary", encoding="utf-8")

    def flush_publisher(**kwargs):
        result = PublishResult(drive_action="created", drive_path=str(tmp_path / "drive" / "flush.md"))
        publish._classify_after_drive_write(result, destination.parent)
        return result

    monkeypatch.setattr(publish, "publish_final_md", flush_publisher)
    assert len(publish.flush_staging(md_root=str(tmp_path / "md"), base_path=str(tmp_path),
        work_path=str(tmp_path / "work"))) == 1

    # A previously failed classifier entry is retried, but the provider is never called.
    jev_classify.classify_if_needed(source, index_path, provider=provider)
    summary = jev_classify.retry_errors(index_path, source_dir, provider=provider)
    assert summary.provider_calls == 0 and provider.calls == 0

    # run_sync_safe performs its normal inventory/write work and skips only classification.
    md_root = tmp_path / "obsidian" / "2026_10_23"
    md_root.mkdir(parents=True)
    (md_root / "channel_title_abcdefghijk_ko_5-mini.md").write_text(
        "---\nformat_version: 4.1\ntitle: title\n---\n\n# title\n", encoding="utf-8")
    sync_root = tmp_path / "YT_summary_sync"
    (sync_root / "source").mkdir(parents=True)
    (sync_root / "legacy").mkdir()
    monkeypatch.setenv("P03_DRIVE_BACKEND", "desktop")
    monkeypatch.setenv("P03_MANIFEST_WRITER", "desktop")
    monkeypatch.setenv("P03_DRIVE_SYNC_ENABLED", "1")
    monkeypatch.setenv("BASE_PATH", str(tmp_path))
    monkeypatch.setenv("WORK_PATH", str(tmp_path / "work"))
    monkeypatch.setenv("OUTPUT_MD_PATH", str(tmp_path / "obsidian"))
    monkeypatch.setenv("P03_DRIVE_SYNC_ROOT", str(sync_root))
    result = sync.run_sync_safe(migrate_legacy=False)
    assert result.errors == 0 and result.created == 1
    assert paid_calls == []

def _openai_retry_client(monkeypatch, run_budget, *, error_kind, status_code=500):
    httpx = run_budget.httpx
    from openai import OpenAI
    import openai._base_client as sdk_base

    calls = []
    delays = []
    monkeypatch.setattr(sdk_base, "random", lambda: 0)
    monkeypatch.setattr(sdk_base.time, "sleep", delays.append)

    def handler(request):
        calls.append(request)
        if error_kind == "connection":
            raise httpx.ConnectError("connection dropped", request=request)
        headers = {"retry-after": "120"} if error_kind == "retry_after" else {}
        return httpx.Response(status_code, headers=headers,
                               json={"error": {"message": "temporary failure", "type": "server_error"}})

    transport = httpx.MockTransport(handler)
    client = OpenAI(api_key="test", http_client=run_budget.make_http_client(transport=transport))
    return client, calls, delays


@pytest.mark.parametrize("error_kind,status_code", [
    ("status", 500), ("retry_after", 429), ("connection", 500),
])
@pytest.mark.parametrize("endpoint", ["chat", "responses"])
def test_sdk_and_origin_app_retry_counts_match_below_deadline(
    monkeypatch, error_kind, status_code, endpoint,
):
    import openai._base_client as sdk_base
    import openai
    import run_budget
    import stt_function_v3 as stt
    from llm_responses import ResponsesCallError, call_responses

    monkeypatch.setattr(run_budget, "_deadline", None)
    client, calls, delays = _openai_retry_client(
        monkeypatch, run_budget, error_kind=error_kind, status_code=status_code)
    sdk_delays = ([120.0, 120.0] if error_kind == "retry_after" and
                  openai.__version__.startswith("3.") else [0.5, 1.0])
    try:
        if endpoint == "chat":
            with pytest.raises(Exception):
                stt.response_text_o1("question", client)
            expected_calls = 3
            expected_delays = sdk_delays
        else:
            with pytest.raises(ResponsesCallError):
                call_responses(client, "gpt-4.1", "instructions", "input")
            expected_calls = 9
            expected_delays = sdk_delays + [0.25] + sdk_delays + [0.5] + sdk_delays
        assert len(calls) == expected_calls
        assert delays == expected_delays
    finally:
        client.close()


def test_production_clients_restore_sdk_default_retries_and_use_transport_gate():
    import inspect
    import main
    import stt_function_v3 as stt

    assert "max_retries=0" not in inspect.getsource(main.initialize_clients)
    assert "max_retries=0" not in inspect.getsource(main._create_llm_provider_client)
    assert "run_budget.call_with_retries" not in inspect.getsource(stt)
    assert "http_client=run_budget.make_http_client()" in inspect.getsource(main.initialize_clients)
    assert "run_budget" not in inspect.getsource(stt)


@pytest.mark.parametrize("endpoint", ["chat", "responses"])
def test_deadline_crossed_during_sdk_retry_blocks_next_request_and_defers(monkeypatch, endpoint):
    import run_budget
    httpx = run_budget.httpx
    from openai import OpenAI
    import openai._base_client as sdk_base
    import stt_function_v3 as stt
    from llm_responses import ResponsesCallError, call_responses

    now = [100.0]
    monkeypatch.setattr(run_budget.time, "time", lambda: now[0])
    monkeypatch.setattr(run_budget, "_deadline", 101.0)
    monkeypatch.setattr(sdk_base, "random", lambda: 0)
    monkeypatch.setattr(sdk_base.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds + 1))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={"error": {"message": "temporary", "type": "server_error"}})

    client = OpenAI(api_key="test", http_client=run_budget.make_http_client(
        transport=httpx.MockTransport(handler)))
    try:
        if endpoint == "chat":
            with pytest.raises(run_budget.RunBudgetExpired, match="run budget expired"):
                stt.response_text_o1("question", client)
        else:
            with pytest.raises(ResponsesCallError, match="run budget expired") as exc:
                call_responses(client, "gpt-4.1", "instructions", "input")
            assert exc.value.category == "run_budget_expired"
        assert len(calls) == 1
        from pipeline_models import VideoProcessResult
        import manual_video_input
        deferred = VideoProcessResult.from_legacy_tuple(
            "deferred_run_budget", "abcdefghijk", "run_budget_expired", source_url="https://youtu.be/abcdefghijk")
        assert deferred.retryable is False
    finally:
        client.close()


def test_run_budget_deferral_restores_manual_and_channel_rows_for_next_run(tmp_path):
    import channel_crawl
    import manual_video_input as mvi
    from pipeline_models import VideoProcessResult
    from shared_state_writer import SharedStateWriter

    manual = mvi.read_ledger(tmp_path / "missing.csv")
    manual = mvi.upsert_ledger(
        manual, video_id="manual00001", canonical_url="https://youtu.be/manual00001",
        source_url="https://youtu.be/manual00001", status="retryable_failed",
        reason="prior API failure")
    manual.loc[manual.video_id == "manual00001", "attempts"] = 2
    manual.loc[manual.video_id == "manual00001", "regen_attempts"] = 0
    manual.loc[manual.video_id == "manual00001", "retry_count"] = 0
    manual.loc[manual.video_id == "manual00001", "last_attempt_at"] = "before-dispatch"
    snapshot = mvi.snapshot_dispatch_state(manual, "manual00001")
    claimed = mvi._mark_manual_queue_processing(
        manual.copy(), "manual00001", "https://youtu.be/manual00001", "https://youtu.be/manual00001")
    assert int(claimed.iloc[0].attempts) == 3
    restored = mvi.restore_dispatch_state(claimed, "manual00001", snapshot)
    row = restored.iloc[0]
    assert row.status == "retryable_failed" and int(row.attempts) == 2
    assert int(row.regen_attempts) == 0 and int(row.retry_count) == 0
    assert row.reason == "prior API failure" and row.last_attempt_at == "before-dispatch"
    assert "manual00001" not in mvi.terminal_manual_ids(restored, max_retries=3)
    queued, _ = mvi.prepare_manual_queue(
        ["https://youtu.be/manual00001"], [], pd.DataFrame(), restored, 3,
        processed_video_ids=set(), mark_processing=False)
    assert [item[0] for item in queued] == ["manual00001"]

    queue = pd.DataFrame([{
        "video_id": "channel0001", "status": "failed", "retry_count": 1,
        "last_attempted_at": "before-dispatch", "last_error": "prior failure",
        "done_at": "", "published_at": "2026-10-20", "discovered_at": "2026-10-21",
    }])
    deferred_queue = channel_crawl.apply_result_to_queue(
        queue, "channel0001", "deferred_run_budget", "run budget expired")
    assert deferred_queue.iloc[0].to_dict() == queue.iloc[0].to_dict()
    selected = channel_crawl.select_process_candidates(deferred_queue, max_retries=3)
    assert selected.video_id.tolist() == ["channel0001"]

    persisted_output = []
    writer = SharedStateWriter(
        data_root=str(tmp_path), output_df=pd.DataFrame(), output_df_path="unused",
        save_output_df=lambda frame, _path: persisted_output.append(frame.copy()),
        crawl_queue_df=queue, save_crawl_queue=lambda _root, frame: persisted_output.append(frame.copy()),
        channel_crawl_enabled=True,
    )
    writer.apply(VideoProcessResult(
        video_id="channel0001", source_url="https://youtu.be/channel0001",
        status="deferred_run_budget", error_message="run budget expired"))
    assert len(writer.get_output_df()) == 0
    assert channel_crawl.select_process_candidates(writer.get_crawl_queue_df(), max_retries=3).video_id.tolist() == [
        "channel0001"]


@pytest.mark.parametrize("prior_exists", [False, True], ids=["manual-new-row", "manual-existing-row"])
def test_manual_dispatch_deferral_restores_pre_dispatch_state_and_reselects(tmp_path, prior_exists):
    import manual_video_input as mvi

    video_id = "manual00002"
    url = f"https://youtu.be/{video_id}"
    before = mvi.read_ledger(tmp_path / "missing.csv")
    if prior_exists:
        before = mvi.upsert_ledger(
            before, video_id=video_id, canonical_url=url, source_url=url,
            status="retryable_failed", reason="prior API failure")
        before.loc[before.video_id == video_id, "attempts"] = 2
        before.loc[before.video_id == video_id, "last_attempt_at"] = "before-dispatch"
    expected = before.copy(deep=True)

    snapshot = mvi.snapshot_dispatch_state(before, video_id)
    claimed = mvi._mark_manual_queue_processing(before.copy(), video_id, url, url)
    assert int(claimed.iloc[0].attempts) == (3 if prior_exists else 1)
    restored = mvi.restore_dispatch_state(claimed, video_id, snapshot)
    if prior_exists:
        actual_state = restored.iloc[0][list(mvi.LEDGER_COLUMNS)].astype(str).to_dict()
        expected_state = expected.iloc[0][list(mvi.LEDGER_COLUMNS)].astype(str).to_dict()
        assert actual_state == expected_state
    else:
        assert restored.empty

    selected, _ = mvi.prepare_manual_queue(
        [url], [], pd.DataFrame(), restored, 3, processed_video_ids=set(), mark_processing=False)
    assert [item[0] for item in selected] == [video_id]


def test_legacy_preprocess_expiry_returns_deferral_and_remains_reselectable(tmp_path, monkeypatch):
    import main
    import manual_video_input as mvi
    import run_budget

    video_id = "legacy00001"
    url = f"https://youtu.be/{video_id}"
    full = tmp_path / f"{video_id}_full.txt"
    full.write_text("existing transcript", encoding="utf-8")
    monkeypatch.setenv("P03_DRIVE_BACKEND", "desktop")
    monkeypatch.setattr(main, "_build_transcript_cache", lambda _config: None)
    monkeypatch.setattr(main, "_find_exact_durable_full_transcript", lambda *_args: str(full))
    monkeypatch.setattr(main, "_read_durable_full_transcript", lambda *_args: (str(full), "existing transcript"))
    monkeypatch.setattr(main, "_is_requeue_unpublished_video", lambda *_args: False)

    class ExpiringPreprocessor:
        def minimize(self, *_args, **_kwargs):
            raise run_budget.RunBudgetExpired("run budget expired during preprocessing")

    main_llm = main.MainLlmConfig(object(), "model", "provider")
    args = (url, {"WORK_PATH": str(tmp_path), "DATA_ROOT": str(tmp_path), "USE_JOB_WORKSPACE": False},
            None, main_llm, pd.DataFrame(), str(tmp_path), str(tmp_path / "audio"), str(tmp_path / "full"),
            str(tmp_path / "smm"), str(tmp_path / "md"), str(tmp_path / "logs"), str(tmp_path / "hf"))
    result = main.process_single_video(*args, preprocessor=ExpiringPreprocessor())
    assert result.status == "deferred_run_budget"
    assert result.retryable is False

    before = mvi.read_ledger(tmp_path / "missing.csv")
    snapshot = mvi.snapshot_dispatch_state(before, video_id)
    claimed = mvi._mark_manual_queue_processing(before, video_id, url, url)
    restored = mvi.restore_dispatch_state(claimed, video_id, snapshot)
    assert restored.empty
    selected, _ = mvi.prepare_manual_queue(
        [url], [], pd.DataFrame(), restored, 3, processed_video_ids=set(), mark_processing=False)
    assert [item[0] for item in selected] == [video_id]


def test_legacy_summary_guard_raises_dedicated_deferral():
    import main
    import run_budget

    main_llm = main.MainLlmConfig(object(), "model", "provider")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(run_budget, "expired", lambda: True)
        with pytest.raises(run_budget.RunBudgetExpired):
            main_llm.summarize("text", "file", "prompt")
    finally:
        monkeypatch.undo()


def test_run_budget_http_client_keeps_sdk_redirect_defaults(monkeypatch):
    import run_budget
    from openai import OpenAI
    httpx = run_budget.httpx

    seen = []
    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/v1/responses":
            return httpx.Response(307, headers={"location": "/v1/responses-final"})
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "created_at": 1,
            "status": "completed", "model": "gpt-4.1",
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "redirect worked", "annotations": []}
            ]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        })

    client = OpenAI(api_key="test", http_client=run_budget.make_http_client(
        transport=httpx.MockTransport(handler)))
    try:
        result = client.responses.create(model="gpt-4.1", input="hello")
        assert result.output_text == "redirect worked"
        assert seen == ["/v1/responses", "/v1/responses-final"]
    finally:
        client.close()


def test_run_budget_expiry_between_redirect_hops_blocks_redirect(monkeypatch):
    import run_budget
    from openai import OpenAI
    httpx = run_budget.httpx

    now = [100.0]
    monkeypatch.setattr(run_budget.time, "time", lambda: now[0])
    monkeypatch.setattr(run_budget, "_deadline", 101.0)
    seen = []

    def handler(request):
        seen.append(request.url.path)
        now[0] = 102.0
        return httpx.Response(307, headers={"location": "/v1/responses-final"})

    client = OpenAI(api_key="test", http_client=run_budget.make_http_client(
        transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(run_budget.RunBudgetExpired, match="run budget expired"):
            client.responses.create(model="gpt-4.1", input="hello")
        assert seen == ["/v1/responses"]
    finally:
        client.close()
