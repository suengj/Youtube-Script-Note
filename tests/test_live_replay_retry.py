from datetime import datetime

import pandas as pd

import channel_crawl as cc
from pipeline_models import VideoProcessResult
from shared_state_writer import SharedStateWriter


VIDEO_ID = "liveReplay01"


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


def test_normal_video_queue_behavior_is_unchanged():
    queue = cc.apply_result_to_queue(_queue_row(), VIDEO_ID, "download_failed", "network")
    assert queue.iloc[0]["status"] == "failed"
    assert queue.iloc[0]["retry_count"] == 1
    assert queue.iloc[0]["last_error"] == "network"

    success = cc.apply_result_to_queue(queue, VIDEO_ID, "success", None)
    assert success.iloc[0]["status"] == "done"
    assert success.iloc[0]["retry_count"] == 1


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
    }])
    monkeypatch.setattr(cc, "fetch_channel_via_api", lambda *_args, **_kwargs: (
        [{"video_id": VIDEO_ID, "url": f"https://youtu.be/{VIDEO_ID}",
          "published_at": "2026-10-21T00:00:00Z"}], "매경 라이브", uploads_id
    ))

    queue, candidates, _ = cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture"},
        pd.DataFrame([{"v_id": VIDEO_ID, "status": "live_scheduled"}]),
    )

    assert candidates["video_id"].tolist() == [VIDEO_ID]
    assert queue.iloc[0]["status"] == "queued"
    channels = cc.load_channel_df(str(tmp_path))
    assert channels[0]["last_discovered_published_at"] == "2026-10-21T00:00:00Z"
    persisted = cc.load_crawl_queue_df(str(tmp_path))
    assert persisted.iloc[0]["video_id"] == VIDEO_ID

