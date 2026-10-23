from datetime import datetime

import pandas as pd
import pytest

import channel_crawl as cc
import main
from pipeline_models import VideoProcessResult
from shared_state_writer import SharedStateWriter


VIDEO_ID = "liveReplay1"


def _queue_row(**updates):
    row = {column: "" for column in cc.CRAWL_QUEUE_COLUMNS}
    row.update(
        video_id=VIDEO_ID,
        url=f"https://www.youtube.com/watch?v={VIDEO_ID}",
        status="queued",
        retry_count=0,
        discovered_at="2026-10-22T00:00:00Z",
        published_at="2026-10-22T00:00:00Z",
    )
    row.update(updates)
    return pd.DataFrame([row])


def test_live_stays_retryable_without_incrementing_retry_count():
    queue = cc.apply_result_to_queue(_queue_row(retry_count=2), VIDEO_ID, "live_scheduled", None)

    assert queue.iloc[0]["status"] == "queued"
    assert queue.iloc[0]["retry_count"] == 2
    assert queue.iloc[0]["last_error"] == "live_scheduled"
    assert queue.iloc[0]["done_at"] == ""
    candidates = cc.select_process_candidates(queue, max_retries=3, live_retry_min_interval_min=0)
    assert candidates["video_id"].tolist() == [VIDEO_ID]

    reconciled = cc.reconcile_queue_with_output_df(
        queue,
        pd.DataFrame([{"v_id": VIDEO_ID, "status": "live_scheduled"}]),
    )
    assert reconciled.iloc[0]["status"] == "queued"
    assert reconciled.iloc[0]["last_error"] == "live_scheduled"


def test_live_candidate_with_nan_last_attempted_at_is_kept():
    queue = _queue_row(last_error="live_scheduled", last_attempted_at=float("nan"))

    candidates = cc.select_process_candidates(queue, max_retries=3)

    assert candidates["video_id"].tolist() == [VIDEO_ID]


def test_parse_iso_date_returns_none_for_nan():
    assert cc._parse_iso_date(float("nan")) is None


def test_later_vod_success_completes_queue_and_is_written_once():
    output = pd.DataFrame(columns=["date", "url", "v_id", "status"])
    saved = []
    queue = _queue_row()
    writer = SharedStateWriter(
        data_root=".",
        output_df=output,
        output_df_path="output.csv",
        save_output_df=lambda df, _path: saved.append(df.copy()),
        crawl_queue_df=queue,
        save_crawl_queue=lambda _root, _df: None,
        channel_crawl_enabled=True,
    )

    writer.apply(VideoProcessResult(video_id=VIDEO_ID, source_url="url", status="live_scheduled"))
    writer.apply(VideoProcessResult(video_id=VIDEO_ID, source_url="url", status="success"))

    output = writer.get_output_df()
    assert (output["status"] == "success").sum() == 1
    assert output["v_id"].tolist() == [VIDEO_ID, VIDEO_ID]
    result_queue = writer.get_crawl_queue_df()
    assert result_queue.iloc[0]["status"] == "done"
    assert result_queue.iloc[0]["retry_count"] == 0
    assert result_queue.iloc[0]["last_error"] == ""
    assert len(saved) == 2


def test_live_replay_expiry_marks_done_with_explicit_reason():
    old_queue = _queue_row(
        status="queued",
        last_error="live_scheduled",
        discovered_at="2026-10-18T00:00:00Z",
        last_attempted_at="2026-10-21T00:00:00Z",
    )

    expired = cc.expire_live_replay_rows(
        old_queue, wait_hours=72, now=datetime(2026, 10, 22, 12, 0, 0)
    )

    assert expired.iloc[0]["status"] == "done"
    assert expired.iloc[0]["last_error"] == "live_no_replay"
    assert expired.iloc[0]["done_at"] == "2026-10-22T12:00:00Z"
    assert cc.select_process_candidates(expired, max_retries=3).empty


def test_probe_failures_preserve_replay_wait_through_reconciliation_and_expiry():
    queue = _queue_row(last_error="live_scheduled", retry_count=2,
                       discovered_at="2026-10-18T00:00:00Z")
    for _ in range(3):
        queue = cc.apply_result_to_queue(queue, VIDEO_ID, "download_failed", "processing")
        assert queue.iloc[0]["status"] == "queued"
        assert queue.iloc[0]["last_error"] == "live_scheduled"
        assert queue.iloc[0]["retry_count"] == 2
        queue = cc.reconcile_queue_with_output_df(
            queue, pd.DataFrame([{"v_id": VIDEO_ID, "status": "download_failed"}])
        )
        assert queue.iloc[0]["status"] == "queued"
        assert queue.iloc[0]["last_error"] == "live_scheduled"
    expired = cc.expire_live_replay_rows(
        queue, wait_hours=72, now=datetime(2026, 10, 22, 12, 0, 0)
    )
    assert expired.iloc[0]["status"] == "done"
    assert expired.iloc[0]["last_error"] == "live_no_replay"


def test_normal_video_queue_behavior_is_unchanged():
    queue = cc.apply_result_to_queue(_queue_row(), VIDEO_ID, "download_failed", "network")
    assert queue.iloc[0]["status"] == "failed"
    assert queue.iloc[0]["retry_count"] == 1
    assert queue.iloc[0]["last_error"] == "network"

    success = cc.apply_result_to_queue(queue, VIDEO_ID, "success", None)
    assert success.iloc[0]["status"] == "done"
    assert success.iloc[0]["retry_count"] == 1


@pytest.mark.parametrize("status", ["success", "file_error"])
def test_shared_state_writer_mutates_blank_queue_timestamps_loaded_from_csv(tmp_path, status):
    row = {column: "" for column in cc.CRAWL_QUEUE_COLUMNS}
    row.update(video_id=VIDEO_ID, url="https://youtu.be/" + VIDEO_ID, status="queued", retry_count=0)
    pd.DataFrame([row], columns=cc.CRAWL_QUEUE_COLUMNS).to_csv(
        tmp_path / cc.CRAWL_QUEUE_FILENAME, index=False, encoding="utf-8-sig"
    )
    loaded_queue = cc.load_crawl_queue_df(str(tmp_path))
    assert loaded_queue["done_at"].dtype == object
    assert loaded_queue["last_attempted_at"].dtype == object

    writer = SharedStateWriter(
        data_root=str(tmp_path),
        output_df=pd.DataFrame(columns=["date", "url", "v_id", "status"]),
        output_df_path=str(tmp_path / "output.csv"),
        save_output_df=lambda *_: None,
        crawl_queue_df=loaded_queue,
        save_crawl_queue=lambda *_: None,
        channel_crawl_enabled=True,
    )
    writer.apply(VideoProcessResult(
        video_id=VIDEO_ID, source_url="https://youtu.be/" + VIDEO_ID, status=status,
        error_message="synthetic file error" if status == "file_error" else None,
    ))
    result = writer.get_crawl_queue_df().iloc[0]
    assert result["last_attempted_at"]
    assert result["status"] == ("done" if status == "success" else "failed")


def test_incomplete_discovery_preserves_cursor_for_next_run(tmp_path, monkeypatch):
    channel_id = "UC" + "a" * 22
    uploads_id = "UU" + "a" * 22
    old_cursor = "2026-10-20T00:00:00Z"
    cc.save_channel_df(str(tmp_path), [{
        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
        "channel_name": "Channel", "usage_channel": "Channel", "channel_id": channel_id,
        "uploads_playlist_id": uploads_id,
        "last_processed_published_at": old_cursor,
        "last_discovered_published_at": "",
    }])
    calls = []

    def fetch(_key, _cid, uploads_id=None, cursor_dt=None, discovery_status=None):
        calls.append(cursor_dt)
        if len(calls) == 1:
            discovery_status["complete"] = False  # pagination stopped after a partial page
            entries = [{"video_id": "latest00001", "url": "https://youtu.be/latest00001",
                        "published_at": "2026-10-23T00:00:00Z"}]
        else:
            # The item missed by the partial first page is only returned when the
            # saved cursor remains at October 20.
            entries = []
            if cursor_dt and cursor_dt < cc._parse_iso_date("2026-10-22T00:00:00Z"):
                entries.append({"video_id": "missed00001", "url": "https://youtu.be/missed00001",
                                "published_at": "2026-10-22T00:00:00Z"})
            entries.append({"video_id": "latest00001", "url": "https://youtu.be/latest00001",
                            "published_at": "2026-10-23T00:00:00Z"})
        return entries, "Channel", uploads_id

    monkeypatch.setattr(cc, "fetch_channel_via_api", fetch)
    output = pd.DataFrame(columns=["v_id", "status"])
    report = {}
    first_queue, _, _ = cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture", "FILTERING_SHORTS_MINUTES": 0},
        output, discovery_report=report,
    )
    assert report["complete"] is False
    assert report["cursor_blocked_channels"] == [channel_id]
    first_done_queue = first_queue.copy()
    first_done_queue["status"] = "done"
    cc.update_channel_last_processed_from_queue(
        str(tmp_path), first_done_queue, exclude_channel_ids=report["cursor_blocked_channels"]
    )
    first_channel = cc.load_channel_df(str(tmp_path))[0]
    assert first_channel["last_discovered_published_at"] == ""
    assert first_channel["last_processed_published_at"] == old_cursor

    second_queue, _, _ = cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture", "FILTERING_SHORTS_MINUTES": 0},
        output, discovery_report=report,
    )
    assert calls[1] == cc._parse_iso_date(old_cursor)
    assert report["complete"] is True
    assert "missed00001" in set(second_queue["video_id"].astype(str))


def test_malformed_discovery_id_blocks_both_cursors_until_recovered(tmp_path, monkeypatch):
    channel_id = "UC" + "a" * 22
    uploads_id = "UU" + "a" * 22
    old_cursor = "2026-10-20T00:00:00Z"
    cc.save_channel_df(str(tmp_path), [{
        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
        "channel_name": "Channel", "usage_channel": "Channel", "channel_id": channel_id,
        "uploads_playlist_id": uploads_id,
        "last_processed_published_at": old_cursor,
        "last_discovered_published_at": "",
    }])
    pages = [
        {"items": [
            {"snippet": {"resourceId": {"videoId": "latest00001"},
                          "publishedAt": "2026-10-23T00:00:00Z"}},
            {"snippet": {"resourceId": {}, "publishedAt": "2026-10-22T00:00:00Z"}},
        ]},
        {"items": [
            {"snippet": {"resourceId": {"videoId": "missed00001"},
                          "publishedAt": "2026-10-22T00:00:00Z"}},
            {"snippet": {"resourceId": {"videoId": "latest00001"},
                          "publishedAt": "2026-10-23T00:00:00Z"}},
        ]},
    ]
    api_calls = []

    def api_request(_key, endpoint, _params):
        assert endpoint == "playlistItems"
        api_calls.append(endpoint)
        return pages[min((len(api_calls) - 1), len(pages) - 1)]

    monkeypatch.setattr(cc, "_api_request", api_request)
    output = pd.DataFrame(columns=["v_id", "status"])
    report = {}

    first_queue, _, _ = cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture", "FILTERING_SHORTS_MINUTES": 0},
        output, discovery_report=report,
    )

    assert report["complete"] is False
    assert report["unresolved"] == 1
    assert report["cursor_blocked_channels"] == [channel_id]
    first_done_queue = first_queue.copy()
    first_done_queue["status"] = "done"
    cc.update_channel_last_processed_from_queue(
        str(tmp_path), first_done_queue, exclude_channel_ids=report["cursor_blocked_channels"]
    )
    first_channel = cc.load_channel_df(str(tmp_path))[0]
    assert first_channel["last_discovered_published_at"] == ""
    assert first_channel["last_processed_published_at"] == old_cursor

    second_queue, _, _ = cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture", "FILTERING_SHORTS_MINUTES": 0},
        output, discovery_report=report,
    )

    assert report["complete"] is True
    assert report["unresolved"] == 0
    assert "missed00001" in set(second_queue["video_id"].astype(str))
    assert cc.load_channel_df(str(tmp_path))[0]["last_discovered_published_at"] == "2026-10-23T00:00:00Z"


def test_discovery_requeues_prior_live_output_and_keeps_cursor_backed_by_queue(tmp_path, monkeypatch):
    channel_id = "UC" + "a" * 22
    uploads_id = "UU" + "a" * 22
    cc.save_channel_df(str(tmp_path), [{
        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
        "channel_name": "매경 라이브",
        "usage_channel": "매경 라이브",
        "channel_id": channel_id,
        "uploads_playlist_id": uploads_id,
        "last_processed_published_at": "2026-10-20T00:00:00Z",
        "last_discovered_published_at": "2026-10-20T00:00:00Z",
        "auto_sub_only": "true",
    }])
    monkeypatch.setattr(cc, "fetch_channel_via_api", lambda *_args, **_kwargs: (
        [{"video_id": VIDEO_ID, "url": f"https://youtu.be/{VIDEO_ID}",
          "published_at": "2026-10-21T00:00:00Z"}] * 2, "매경 라이브", uploads_id
    ))
    monkeypatch.setattr(cc, "_fetch_video_durations", lambda *_: ({VIDEO_ID: "PT0S"}, {}))

    queue, candidates, _ = cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture", "FILTERING_SHORTS_MINUTES": 3},
        pd.DataFrame([{"v_id": VIDEO_ID, "status": "live_scheduled"}]),
    )

    assert candidates["video_id"].tolist() == [VIDEO_ID]
    assert candidates.iloc[0]["is_shorts"] == "false"
    assert candidates.iloc[0]["auto_sub_only"] == "true"
    assert queue.iloc[0]["status"] == "queued"
    channels = cc.load_channel_df(str(tmp_path))
    assert channels[0]["last_discovered_published_at"] == "2026-10-21T00:00:00Z"
    persisted = cc.load_crawl_queue_df(str(tmp_path))
    assert persisted.iloc[0]["video_id"] == VIDEO_ID
    assert len(queue[queue["video_id"] == VIDEO_ID]) == 1


def test_rediscovered_live_row_keeps_replay_wait_through_probe_failures_and_expiry(tmp_path, monkeypatch):
    channel_id = "UC" + "c" * 22
    uploads_id = "UU" + "c" * 22
    cc.save_channel_df(str(tmp_path), [{
        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
        "channel_name": "Live", "usage_channel": "Live", "channel_id": channel_id,
        "uploads_playlist_id": uploads_id,
        "last_processed_published_at": "2026-10-20T00:00:00Z",
        "last_discovered_published_at": "2026-10-20T00:00:00Z",
    }])
    monkeypatch.setattr(cc, "fetch_channel_via_api", lambda *_args, **_kwargs: (
        [{"video_id": VIDEO_ID, "url": f"https://youtu.be/{VIDEO_ID}",
          "published_at": "2026-10-21T00:00:00Z"}], "Live", uploads_id
    ))
    monkeypatch.setattr(cc, "_fetch_video_durations", lambda *_: ({VIDEO_ID: "PT0S"}, {}))

    queue, candidates, _ = cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture", "FILTERING_SHORTS_MINUTES": 3},
        pd.DataFrame([{"v_id": VIDEO_ID, "status": "live_scheduled"}]),
    )
    assert candidates["video_id"].tolist() == [VIDEO_ID]
    assert queue.iloc[0]["last_error"] == "live_scheduled"
    queue.loc[:, "discovered_at"] = "2026-10-18T00:00:00Z"

    for _ in range(3):
        queue = cc.apply_result_to_queue(queue, VIDEO_ID, "download_failed", "processing")
        queue = cc.reconcile_queue_with_output_df(
            queue, pd.DataFrame([{"v_id": VIDEO_ID, "status": "download_failed"}])
        )
    assert queue.iloc[0]["status"] == "queued"
    assert queue.iloc[0]["retry_count"] == 0
    assert candidates["video_id"].tolist() == [VIDEO_ID]

    expired = cc.expire_live_replay_rows(
        queue, wait_hours=72, now=datetime(2026, 10, 22, 12, 0, 0)
    )
    assert expired.iloc[0]["status"] == "done"
    assert expired.iloc[0]["last_error"] == "live_no_replay"


def test_reconciliation_keeps_rediscovered_replay_classified_as_shorts_terminal(tmp_path, monkeypatch):
    channel_id = "UC" + "d" * 22
    uploads_id = "UU" + "d" * 22
    cc.save_channel_df(str(tmp_path), [{
        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
        "channel_name": "Live", "usage_channel": "Live", "channel_id": channel_id,
        "uploads_playlist_id": uploads_id,
        "last_processed_published_at": "2026-10-20T00:00:00Z",
        "last_discovered_published_at": "2026-10-20T00:00:00Z",
    }])
    monkeypatch.setattr(cc, "fetch_channel_via_api", lambda *_args, **_kwargs: (
        [{"video_id": VIDEO_ID, "url": f"https://youtu.be/{VIDEO_ID}",
          "published_at": "2026-10-21T00:00:00Z"}], "Live", uploads_id
    ))
    monkeypatch.setattr(cc, "_fetch_video_durations", lambda *_: ({VIDEO_ID: "PT2M"}, {}))
    live_output = pd.DataFrame([{"v_id": VIDEO_ID, "status": "live_scheduled"}])

    queue, candidates, shorts_rows = cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture", "FILTERING_SHORTS_MINUTES": 3}, live_output,
    )
    assert candidates.empty
    assert queue.iloc[0]["status"] == "done"
    assert queue.iloc[0]["last_error"] == cc.SHORTS_STATUS
    assert shorts_rows[0]["status"] == cc.SHORTS_STATUS

    # main.py suppresses a duplicate shorts output row because this ID already
    # exists, so the latest output remains live_scheduled on the next run.
    reconciled = cc.reconcile_queue_with_output_df(queue, live_output)
    assert reconciled.iloc[0]["status"] == "done"
    assert reconciled.iloc[0]["last_error"] == cc.SHORTS_STATUS
    assert cc.select_process_candidates(reconciled, max_retries=3).empty


def test_process_single_video_retries_prior_live_output(tmp_path, monkeypatch):
    monkeypatch.setattr(main.stt, "extract_youtube_id", lambda _url: VIDEO_ID)
    monkeypatch.setattr(main, "_find_exact_durable_full_transcript", lambda *_: None)
    monkeypatch.setattr(main, "_read_durable_full_transcript", lambda *_: (None, None))
    download_calls = []
    monkeypatch.setattr(main.stt, "yt_downloader", lambda **_kwargs: download_calls.append(True))
    result = main.process_single_video(
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        {"WORK_PATH": str(tmp_path), "USE_JOB_WORKSPACE": False, "TRANSCRIPT_CACHE_ENABLED": False},
        object(), object(), pd.DataFrame([{"v_id": VIDEO_ID, "status": "live_scheduled"}]),
        str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path),
        str(tmp_path), str(tmp_path),
    )
    assert download_calls == [True]
    assert result.status == "download_failed"


@pytest.mark.parametrize("terminal_status", ["passed_shorts", "video_unavailable", "skipped_auto_subs_only"])
def test_process_single_video_skips_terminal_prior_status(tmp_path, monkeypatch, terminal_status):
    monkeypatch.setattr(main.stt, "extract_youtube_id", lambda _url: VIDEO_ID)
    monkeypatch.setattr(main, "_find_exact_durable_full_transcript", lambda *_: None)
    monkeypatch.setattr(main.stt, "yt_downloader", lambda **_: pytest.fail("terminal video must not download"))
    result = main.process_single_video(
        f"https://www.youtube.com/watch?v={VIDEO_ID}", {"USE_JOB_WORKSPACE": False},
        object(), object(), pd.DataFrame([{"v_id": VIDEO_ID, "status": terminal_status}]),
        str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path),
        str(tmp_path), str(tmp_path),
    )
    assert result.status == "already_existed"


def test_duplicate_candidate_ids_and_live_retry_throttle(tmp_path, monkeypatch):
    channel_id = "UC" + "b" * 22
    uploads_id = "UU" + "b" * 22
    cc.save_channel_df(str(tmp_path), [{
        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
        "channel_name": "Live", "channel_id": channel_id,
        "uploads_playlist_id": uploads_id,
        "last_processed_published_at": "2026-10-20T00:00:00Z",
        "last_discovered_published_at": "2026-10-20T00:00:00Z",
    }])
    monkeypatch.setattr(cc, "fetch_channel_via_api", lambda *_args, **_kwargs: (
        [{"video_id": VIDEO_ID, "url": f"https://youtu.be/{VIDEO_ID}",
          "published_at": "2026-10-21T00:00:00Z"}] * 2, "Live", uploads_id
    ))
    queue, candidates, _ = cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture", "LIVE_RETRY_MIN_INTERVAL_MIN": 60},
        pd.DataFrame([{"v_id": VIDEO_ID, "status": "live_scheduled"}]),
    )
    assert queue["video_id"].tolist() == [VIDEO_ID]
    assert candidates["video_id"].tolist() == [VIDEO_ID]
    throttled = cc.select_process_candidates(
        _queue_row(last_error="live_scheduled", last_attempted_at="2026-10-22T11:30:00Z"),
        max_retries=3, live_retry_min_interval_min=60,
        now=datetime(2026, 10, 22, 12, 0, 0),
    )
    assert throttled.empty


def test_auto_sub_only_metadata_is_passed_to_processing_config():
    url = f"https://youtu.be/{VIDEO_ID}"
    config = main._apply_channel_video_metadata(
        {}, url, set(), {}, {}, {url: True}, {url: "Channel"}, {url: "UC123"}
    )
    assert config["auto_subs_only"] is True
    assert config["usage_channel"] == "Channel"
    assert config["channel_id"] == "UC123"

