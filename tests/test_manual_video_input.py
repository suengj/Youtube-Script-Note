from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import manual_video_input as mvi  # noqa: E402
from scripts.import_manual_urls_from_sheet import import_rows  # noqa: E402


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abcdefghijk&si=abc&list=PL123&index=2&t=5",
    "https://youtu.be/abcdefghijk?si=abc",
    "https://youtube.com/shorts/abcdefghijk?feature=share",
    "https://m.youtube.com/live/abcdefghijk",
    "https://music.youtube.com/embed/abcdefghijk?start=3",
])
def test_video_url_forms_share_canonical_video_id(url):
    result = mvi.normalize_youtube_url(url)
    assert result == {"kind": "video", "video_id": "abcdefghijk",
                      "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk", "reason": ""}


@pytest.mark.parametrize(("url", "kind"), [
    ("https://youtube.com/@somehandle/videos", "channel"),
    ("https://youtube.com/channel/UC123", "channel"),
    ("https://youtube.com/c/somechannel", "channel"),
    ("https://youtube.com/user/someuser", "channel"),
    ("https://youtube.com/playlist?list=PL123", "playlist"),
    ("https://example.com/watch?v=abcdefghijk", "unsupported"),
])
def test_non_video_input_classification(url, kind):
    assert mvi.normalize_youtube_url(url)["kind"] == kind


def test_ledger_atomic_schema_and_upsert(tmp_path):
    path = tmp_path / "manual_input_status.csv"
    ledger = mvi.read_ledger(path)
    ledger = mvi.upsert_ledger(ledger, video_id="abcdefghijk", canonical_url="https://youtube.com/watch?v=abcdefghijk",
                               source_url="https://youtu.be/abcdefghijk", status="processing", attempted=True)
    ledger = mvi.upsert_ledger(ledger, video_id="abcdefghijk", source_url="https://youtube.com/shorts/abcdefghijk",
                               status="retryable_failed", reason="NO_AUTO_CAPTION")
    mvi.save_ledger(ledger, path)
    reloaded = mvi.read_ledger(path)
    row = reloaded.iloc[0]
    assert row.status == "retryable_failed" and row.attempts == 1
    assert "youtube.com/shorts" in row.source_urls and row.reason == "NO_AUTO_CAPTION"
    assert list(reloaded.columns) == mvi.LEDGER_COLUMNS


def test_importer_locates_headers_and_preserves_existing_rows_idempotently(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    input_path = data / "input_df.csv"
    input_path.write_text("date,url,category,title,source\n2024-01-01,https://youtu.be/zzzzzzzzzzz,old,Old title,manual\n", encoding="cp949")
    rows = [
        ["Updated/Published Time/", "URL", "Title", "Source", "Download 여부"],
        ["2025-01-01", "https://www.youtube.com/watch?v=abcdefghijk&si=a", "First", "Pocket", "gpt"],
        ["2025-01-02", "https://youtu.be/abcdefghijk", "Duplicate", "Zapier", "gpt"],
        ["2025-01-03", "https://youtube.com/@handle", "Channel", "Pocket", ""],
        ["2025-01-04", "https://youtube.com/playlist?list=PL123", "List", "Pocket", ""],
        ["2025-01-05", "https://elsewhere.invalid/", "Bad", "Pocket", ""],
    ]
    summary = import_rows(rows, input_path, data)
    assert summary["new"] == 1 and summary["video"] == 2
    frame = pd.read_csv(input_path, encoding="cp949", dtype=str).fillna("")
    assert len(frame) == 2
    assert frame.iloc[0]["url"].endswith("zzzzzzzzzzz")
    assert frame.iloc[1]["date"] == "2025-01-01"
    assert frame.iloc[1]["title"] == "First" and frame.iloc[1]["source"] == "Pocket"
    assert import_rows(rows, input_path, data)["new"] == 0


def test_importer_reports_success_and_queued_ids(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / "input_df.csv").write_text("date,url\n,https://youtu.be/queued00001\n", encoding="cp949")
    pd.DataFrame([{"v_id": "success0001", "url": "u", "status": "success"}]).to_csv(data / "output_df_new.csv", index=False)
    rows = [["URL", "Source", "Download 여부", "Title"],
            ["https://youtu.be/success0001", "", "", "Success"],
            ["https://youtu.be/queued00001", "", "", "Queued"],
            ["https://youtu.be/newvideo001", "", "", "New"]]
    result = import_rows(rows, data / "input_df.csv", data, dry_run=True)
    output = capsys.readouterr().out
    assert result["new"] == 1
    assert "already-success: 1" in output and "already-queued: 1" in output
    assert len(pd.read_csv(data / "input_df.csv", encoding="cp949")) == 1


def test_importer_does_not_treat_historical_channel_failure_as_queued(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    input_path = data / "input_df.csv"
    input_path.write_text("date,url\n", encoding="cp949")
    pd.DataFrame([{"v_id": "failed00001", "url": "https://youtu.be/failed00001", "status": "download_failed"}]).to_csv(
        data / "output_df_new.csv", index=False)
    rows = [["URL", "Source", "Download 여부"], ["https://youtu.be/failed00001", "", ""]]
    result = import_rows(rows, input_path, data)
    assert result["new"] == 1
    assert "failed00001" in pd.read_csv(input_path, encoding="cp949", dtype=str).iloc[0].url


def test_retry_limit_uses_attempt_count(tmp_path):
    ledger = mvi.read_ledger(tmp_path / "missing.csv")
    for _ in range(2):
        ledger = mvi.upsert_ledger(ledger, video_id="abcdefghijk", status="processing", attempted=True)
    assert int(ledger.iloc[0].attempts) == 2


def test_manual_queue_dedupes_by_id_preserves_channel_processing_and_skips_success():
    rows = [
        "https://youtu.be/abcdefghijk?si=x",
        "https://youtube.com/watch?v=abcdefghijk&list=PL1",
        "https://youtube.com/shorts/chan0000001",
        "https://youtube.com/watch?v=success0001",
        "https://youtube.com/playlist?list=PL123",
    ]
    output = pd.DataFrame([{"v_id": "success0001", "status": "success"}])
    ledger = mvi.read_ledger("/missing/manual.csv")
    queued, ledger = mvi.prepare_manual_queue(rows, ["https://youtube.com/live/chan0000001"], output, ledger, 2)
    assert [item[0] for item in queued] == ["abcdefghijk", "chan0000001"]
    assert ledger.loc[ledger.video_id == "chan0000001", "status"].iloc[0] == "processing"
    assert [item[0] for item in queued].count("chan0000001") == 1
    assert ledger.loc[ledger.video_id == "success0001", "status"].iloc[0] == "skipped_policy"
    assert ledger.loc[ledger.status == "unsupported", "reason"].str.contains("playlist").any()


def test_manual_queue_keeps_failures_retryable_and_caps_attempts():
    ledger = mvi.read_ledger("/missing/manual.csv")
    for _ in range(2):
        ledger = mvi.upsert_ledger(ledger, video_id="abcdefghijk", status="retryable_failed",
                                   reason="temporary", attempted=True)
    queued, ledger = mvi.prepare_manual_queue(["https://youtu.be/abcdefghijk"], [], pd.DataFrame(), ledger, 2)
    assert queued == []
    assert "retry limit reached" in ledger.iloc[0].reason
    ledger = mvi.read_ledger("/missing/manual.csv")
    queued, ledger = mvi.prepare_manual_queue(["https://youtu.be/abcdefghijk"], [], pd.DataFrame(), ledger, 2)
    assert queued[0][0] == "abcdefghijk"


@pytest.mark.parametrize(("status", "expected"), [
    ("NO_AUTO_CAPTION", "retryable_failed"),
    ("download_failed", "retryable_failed"),
    ("file_error", "retryable_failed"),
    ("video_unavailable", "unsupported"),
    ("live_scheduled", "skipped_policy"),
])
def test_pipeline_failure_outcomes_retain_retry_or_reason(status, expected):
    got, reason, _, verified = mvi.ledger_outcome(status, "network/no subtitles")
    assert got == expected and reason
    assert verified == "false"


def test_manual_queue_applies_shorts_and_registered_channel_policies():
    rows = ["https://youtu.be/short000001", "https://youtu.be/restrict001"]
    ledger = mvi.read_ledger("/missing/manual.csv")
    queued, ledger = mvi.prepare_manual_queue(
        rows[:1] + rows[1:], [], pd.DataFrame(), ledger, duration_map={"short000001": "PT45S"},
        shorts_threshold_seconds=180, video_channel_ids={"restrict001": "UCregistered"},
        channel_auto_sub_only={"UCregistered": True})
    assert [x[0] for x in queued] == ["restrict001"]
    assert ledger.loc[ledger.video_id == "short000001", "status"].iloc[0] == "skipped_policy"
    assert "45s <= 180s" in ledger.loc[ledger.video_id == "short000001", "reason"].iloc[0]
    # Caption availability is decided by the downloader, not by channel metadata.
    assert ledger.loc[ledger.video_id == "restrict001", "status"].iloc[0] == "processing"


def test_auto_sub_only_accepts_existing_uploader_or_automatic_captions(monkeypatch):
    import stt_function_v3 as stt
    assert stt._caption_availability({}, {"en": [{"ext": "vtt"}]}, ["en"], True) == (False, True)
    assert stt._caption_availability({"en-orig": [{"ext": "vtt"}]}, {}, ["en"], False) == (True, False)
    assert stt._caption_availability({}, {}, ["en"], True) == (False, False)


def test_drive_failure_and_missing_markdown_never_become_success(tmp_path):
    md = tmp_path / "note.md"
    md.write_text("verified local file", encoding="utf-8")
    assert mvi.ledger_outcome("file_error", "Drive publish failed", str(md), md_exists=True)[0] == "retryable_failed"
    assert mvi.ledger_outcome("success", "", str(tmp_path / "missing.md"), md_exists=False)[0] == "retryable_failed"
    assert mvi.ledger_outcome("success", "", str(md), md_exists=True)[0] == "retryable_failed"
    assert mvi.ledger_outcome("success", "", str(md), md_exists=True, drive_verified=True)[0] == "success"


def test_process_videos_runs_unregistered_manual_video_through_existing_path(tmp_path, monkeypatch):
    import main
    from pipeline_models import VideoProcessResult

    data = tmp_path / "data"
    data.mkdir()
    (data / "input_df.csv").write_text("date,url,category\n2026-10-01,https://youtu.be/abcdefghijk?si=variant,manual\n", encoding="cp949")
    base, md = tmp_path / "base", tmp_path / "md"
    base.mkdir()
    md.mkdir()
    seen = []
    registry_roots = []

    class Writer:
        def __init__(self, **kwargs):
            self.df = kwargs["output_df"]
        def apply(self, result):
            self.df.loc[len(self.df)] = {"date": "2026-10-22", "url": result.source_url,
                                         "v_id": result.video_id, "status": result.status}
            main._save_output_df_with_retry(self.df, str(data / "output_df_new.csv"), na_rep="")
        def get_output_df(self):
            return self.df
        def get_crawl_queue_df(self):
            return None

    class Claims:
        def __init__(self, *_): pass
        def recover_all_stale(self): return []
        def try_claim(self, *_args, **_kwargs): return True
        def release(self, *_): pass

    def fake_process(**kwargs):
        seen.append((kwargs["v_url"], kwargs["config"].get("from_input")))
        return VideoProcessResult(video_id="abcdefghijk", source_url=kwargs["v_url"],
                                  status="download_failed", retryable=True, error_message="temporary network failure")

    monkeypatch.setattr(main, "initialize_clients", lambda config: (None, None, None))
    monkeypatch.setattr(main.channel_crawl, "load_channel_df",
                        lambda root: registry_roots.append(root) or [])
    monkeypatch.setattr(main, "create_transcript_preprocessor", lambda *args: object())
    monkeypatch.setattr(main, "SharedStateWriter", Writer)
    monkeypatch.setattr(main, "ClaimManager", Claims)
    monkeypatch.setattr(main, "process_single_video", fake_process)
    monkeypatch.setattr(main, "_run_batch_cleanup", lambda *args, **kwargs: None)
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)
    monkeypatch.setenv("P03_DRIVE_BACKEND", "desktop")
    monkeypatch.setenv("P03_DRIVE_SYNC_ENABLED", "0")
    config = {
        "BASE_PATH": str(base), "DATA_ROOT": str(data), "WORK_PATH": str(base),
        "HF_HOME": str(tmp_path / "hf"), "OUTPUT_MD_PATH": str(md),
        "CHANNEL_CRAWL": False, "VIDEO_WORKERS": 1,
        "MIN_WAIT_BETWEEN_VIDEOS": 0, "MAX_WAIT_BETWEEN_VIDEOS": 0,
        "EXTENDED_WAIT_INTERVAL": 100, "EXTENDED_WAIT_DURATION": 0,
        "MAX_CONSECUTIVE_FAILURES": 2, "FAILURE_WAIT_MULTIPLIER": 1,
        "CRAWL_QUEUE_MAX_RETRIES": 2,
    }
    main.process_videos(config)
    assert registry_roots == [str(data)]
    assert seen == [("https://www.youtube.com/watch?v=abcdefghijk", True)]
    assert len(pd.read_csv(data / "input_df.csv", encoding="cp949")) == 1
    ledger = mvi.read_ledger(data / "manual_input_status.csv")
    assert ledger.iloc[0].status == "retryable_failed"
    assert "temporary network failure" in ledger.iloc[0].reason


class FakeSheets:
    def __init__(self, headers, rows, *, mismatch=False):
        self.headers = headers
        self.rows = [list(row) for row in rows]
        self.writes = []
        self.write_calls = []
        self.mismatch = mismatch
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, spreadsheetId, range=None, fields=None):
        if range is None:
            return FakeRequest(lambda: {"sheets": [{"properties": {"title": "YT Link"}}]})
        self.last_range = range
        return FakeRequest(lambda: self._get(range))
    def _get(self, range):
        if range == "'YT Link'!1:1": return {"values": [self.headers]}
        if range == "'YT Link'!A:Z":
            rows = [list(row) for row in self.rows]
            if self.mismatch and rows:
                while len(rows[0]) < 3: rows[0].append("")
                rows[0][2] = "wrong"
            return {"values": [self.headers, *rows]}
        col_row = range.split("!")[1]
        import re
        match = re.fullmatch(r"([A-Z]+)(\d+)", col_row)
        col, row = 0, int(match.group(2)) - 2
        for char in match.group(1): col = col * 26 + ord(char) - 64
        value = self.rows[row][col - 1] if row < len(self.rows) and col <= len(self.rows[row]) else ""
        if self.mismatch and col_row.startswith("C"):
            value = "wrong"
        return {"values": [[value]]}
    def batchUpdate(self, spreadsheetId, body):
        self.writes = body["data"]
        self.write_calls.append(body["data"])
        def apply():
            import re
            for update in self.writes:
                m = re.fullmatch(r"'YT Link'!([A-Z]+)(\d+)", update["range"])
                col, row = 0, int(m.group(2)) - 2
                for char in m.group(1): col = col * 26 + ord(char) - 64
                while len(self.rows[row]) < col: self.rows[row].append("")
                self.rows[row][col - 1] = update["values"][0][0]
        return FakeRequest(apply)
    def batchGet(self, spreadsheetId, ranges):
        self.batch_get_calls = getattr(self, "batch_get_calls", []) + [list(ranges)]
        def read():
            import re
            out = []
            for range_ in ranges:
                m = re.fullmatch(r"'YT Link'!([A-Z]+)(\d+)(?::([A-Z]+))?", range_)
                col, row = 0, int(m.group(2)) - 2
                for char in m.group(1): col = col * 26 + ord(char) - 64
                if m.group(3):
                    vals = []
                    for row_i in range(max(0, row), len(self.rows)):
                        val = self.rows[row_i][col - 1] if col <= len(self.rows[row_i]) else ""
                        if self.mismatch and col == 3: val = "wrong"
                        vals.append([val])
                    out.append({"range": range_, "values": vals})
                else:
                    val = self.rows[row][col - 1] if row < len(self.rows) and col <= len(self.rows[row]) else ""
                    if self.mismatch and col == 3: val = "wrong"
                    out.append({"range": range_, "values": [[val]]})
            return {"valueRanges": out}
        return FakeRequest(read)


class FakeRequest:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn() or {}


class MovingFakeSheets(FakeSheets):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read_count = 0
    def get(self, spreadsheetId, range=None, fields=None):
        if range == "'YT Link'!A:Z":
            self.read_count += 1
        if range == "'YT Link'!A:Z" and self.read_count == 2:
            self.moved = True
            self.rows.insert(0, ["https://youtu.be/other000001", "", "victim-model"])
        return super().get(spreadsheetId, range=range, fields=fields)
    def batchGet(self, spreadsheetId, ranges):
        return super().batchGet(spreadsheetId, ranges)
    def batchUpdate(self, spreadsheetId, body):
        # Insert a row after snapshot but before the stale row write.
        if not getattr(self, "moved", False):
            self.moved = True
            self.rows.insert(0, ["https://youtu.be/insert00001", "new", "new-model"])
        return super().batchUpdate(spreadsheetId, body)


def success_ledger(video_id="abcdefghijk", model="gpt-6-luna"):
    ledger = mvi.read_ledger("/missing/manual.csv")
    return mvi.upsert_ledger(ledger, video_id=video_id, canonical_url=f"https://youtube.com/watch?v={video_id}",
                             status="success", drive_verified="verified", model=model)


def test_missing_key_skips_sheet_and_keeps_ledger_pending(tmp_path, monkeypatch):
    ledger = success_ledger()
    monkeypatch.delenv("P03_GOOGLE_SA_JSON", raising=False)
    with pytest.raises(FileNotFoundError):
        mvi.build_sheets_service()
    assert ledger.iloc[0].status == "success" and ledger.iloc[0].sheet_synced == "false"


def test_header_missing_aborts_before_sheet_write():
    service = FakeSheets(["URL", "Source"], [])
    ledger = success_ledger()
    with pytest.raises(ValueError, match="Download 여부"):
        mvi.writeback_success_rows(ledger, service)
    assert service.writes == []


def test_writeback_finds_moved_row_preserves_source_and_formulas():
    service = FakeSheets(["URL", "Source", "Download 여부", "F", "L"], [
        ["https://youtu.be/other000001", "", "", "=F2", "=L2"],
        ["https://youtu.be/abcdefghijk?si=moved", "Pocket", "", "=F3", "=L3"],
    ])
    ledger = success_ledger()
    ledger = mvi.writeback_success_rows(ledger, service)
    assert ledger.iloc[0].sheet_synced == "true"
    assert service.rows[1][1] == "Pocket" and service.rows[1][2] == "gpt-6-luna"
    assert service.rows[1][3:] == ["=F3", "=L3"]
    assert [x["range"] for x in service.writes] == ["'YT Link'!C3"]
    assert all(x["range"].split("!")[1][0] in {"B", "C"} for x in service.writes)


def test_writeback_sets_own_only_for_blank_source_and_uses_raw_two_cells():
    service = FakeSheets(["URL", "Source", "Download 여부", "F", "L"], [
        ["https://youtu.be/abcdefghijk", "", "", "=F2", "=L2"]])
    ledger = mvi.writeback_success_rows(success_ledger(model="fallback-x"), service)
    assert ledger.iloc[0].sheet_synced == "true"
    assert service.rows[0][1:3] == ["own", "fallback-x"]
    assert {x["range"] for x in service.writes} == {"'YT Link'!B2", "'YT Link'!C2"}


def test_writeback_revalidates_moved_row_and_retries_without_corrupting_other_video():
    service = MovingFakeSheets(["URL", "Source", "Download 여부"], [
        ["https://youtu.be/victim00001", "Pocket", "old-model"],
        ["https://youtu.be/abcdefghijk", "", ""],
        ["https://youtu.be/other000001", "Keep", "keep-model"],
    ])
    ledger = mvi.writeback_success_rows(success_ledger(), service)
    assert ledger.iloc[0].sheet_synced == "true"
    by_id = {mvi.normalize_youtube_url(row[0])["video_id"]: row for row in service.rows}
    assert by_id["victim00001"][1:] == ["Pocket", "old-model"]
    assert by_id["other000001"][1:] == ["Keep", "keep-model"]
    assert by_id["abcdefghijk"][1:] == ["own", "gpt-6-luna"]
    assert len(service.write_calls) == 3
    assert service.write_calls[0][0]["range"] == "'YT Link'!C3"
    assert {entry["range"] for entry in service.write_calls[1]} == {"'YT Link'!B3", "'YT Link'!C3"}
    assert "'YT Link'!C4" in {entry["range"] for entry in service.write_calls[2]}


def test_readback_mismatch_keeps_pending_and_retries_next_run():
    ledger = success_ledger()
    mismatch = FakeSheets(["URL", "Source", "Download 여부"], [["https://youtu.be/abcdefghijk", "", ""]], mismatch=True)
    ledger = mvi.writeback_success_rows(ledger, mismatch)
    assert ledger.iloc[0].sheet_synced == "false"
    retry = FakeSheets(["URL", "Source", "Download 여부"], [["https://youtu.be/abcdefghijk", "", ""]])
    ledger = mvi.writeback_success_rows(ledger, retry)
    assert ledger.iloc[0].sheet_synced == "true"


def test_failure_statuses_are_never_written():
    ledger = mvi.upsert_ledger(mvi.read_ledger("/missing/manual.csv"), video_id="abcdefghijk",
                               status="retryable_failed", model="gpt-6-luna")
    service = FakeSheets(["URL", "Source", "Download 여부"], [["https://youtu.be/abcdefghijk", "", ""]])
    mvi.writeback_success_rows(ledger, service)
    assert service.writes == []


def test_staging_fallback_retries_publish_without_llm(tmp_path):
    root = tmp_path / "work" / "output_md_staging"
    staged = root / "2026" / "summary.md"
    staged.parent.mkdir(parents=True)
    staged.write_text("already generated", encoding="utf-8")
    ledger = mvi.upsert_ledger(mvi.read_ledger("/missing/manual.csv"), video_id="abcdefghijk",
                               status="retryable_failed", reason="pending_drive: waiting",
                               md_path=str(staged), model="gpt-6-luna",
                               md_sha256=__import__("hashlib").sha256(b"already generated").hexdigest())
    calls = []
    destination = tmp_path / "verified" / "summary.md"
    destination.parent.mkdir()
    destination.write_text("already generated", encoding="utf-8")
    class Pub:
        drive_ok = True
        drive_path = str(destination)
    def publish(**kwargs):
        calls.append(kwargs)
        assert kwargs["content"] == "already generated"
        assert kwargs["retain_staging"] is True
        return Pub()
    retried = mvi.retry_pending_drive(ledger, base_path=str(tmp_path), work_path=str(tmp_path / "work"),
                                      md_root=str(tmp_path / "md"), publisher=publish)
    assert len(calls) == 1 and retried.iloc[0].status == "success"
    assert retried.iloc[0].model == "gpt-6-luna" and retried.iloc[0].drive_verified == "verified"
    assert not staged.exists()

    class FailedPub:
        drive_ok = False
        drive_path = ""
    staged.write_text("already generated", encoding="utf-8")
    still_pending = mvi.upsert_ledger(ledger, video_id="abcdefghijk", status="retryable_failed",
                                      reason="pending_drive: waiting", md_path=str(staged), attempted=True)
    still_pending = mvi.retry_pending_drive(still_pending, base_path=str(tmp_path),
        work_path=str(tmp_path / "work"), md_root=str(tmp_path / "md"), publisher=lambda **_: FailedPub())
    queued, still_pending = mvi.prepare_manual_queue(["https://youtu.be/abcdefghijk"], [],
                                                    pd.DataFrame(), still_pending, 5)
    assert queued == [] and still_pending.iloc[0].reason.startswith("pending_drive:")


def test_drive_ok_without_verified_content_readback_does_not_complete_manual_ledger(tmp_path):
    staged = tmp_path / "work" / "output_md_staging" / "2026" / "summary.md"
    staged.parent.mkdir(parents=True)
    staged.write_text("right content", encoding="utf-8")
    destination = tmp_path / "drive" / "summary.md"
    destination.parent.mkdir()
    destination.write_text("corrupted content", encoding="utf-8")
    ledger = mvi.upsert_ledger(mvi.read_ledger("/missing/manual.csv"), video_id="abcdefghijk",
                               status="retryable_failed", reason="pending_drive: waiting", md_path=str(staged),
                               md_sha256=__import__("hashlib").sha256(b"right content").hexdigest())
    class Pub:
        drive_ok = True
        drive_action = "unchanged"
        drive_path = str(destination)
    calls = []
    def publish(**kwargs):
        calls.append(kwargs)
        if kwargs.get("force_rewrite"):
            destination.write_text(kwargs["content"], encoding="utf-8")
        return Pub()
    updated = mvi.retry_pending_drive(ledger, base_path=str(tmp_path), work_path=str(tmp_path / "work"),
                                      md_root=str(tmp_path / "md"), publisher=publish)
    assert updated.iloc[0].status == "success" and updated.iloc[0].drive_verified == "verified"
    assert len(calls) == 2 and calls[0]["retain_staging"] and calls[1]["force_rewrite"]
    assert not staged.exists()


def test_pending_drive_path_outside_staging_root_is_reprocessed_not_published(tmp_path):
    staged = tmp_path / "drive" / "source" / "summary.md"
    staged.parent.mkdir(parents=True)
    staged.write_text("expected content", encoding="utf-8")
    ledger = mvi.upsert_ledger(mvi.read_ledger("/missing/manual.csv"), video_id="abcdefghijk",
        status="retryable_failed", reason="pending_drive: readback mismatch", md_path=str(staged),
        md_sha256=__import__("hashlib").sha256(b"expected content").hexdigest())
    calls = []
    class Pub:
        drive_ok = True
        drive_path = str(staged)
    retried = mvi.retry_pending_drive(ledger, base_path=str(tmp_path), work_path=str(tmp_path / "work"),
        md_root=str(tmp_path / "md"), publisher=lambda **kw: calls.append(kw) or Pub())
    assert not calls and retried.iloc[0].status == "retryable_failed"
    assert "reprocessing required" in retried.iloc[0].reason


def test_missing_stage_does_not_change_root_for_following_retained_entry(tmp_path, monkeypatch):
    root = tmp_path / "work" / "output_md_staging"
    existing = root / "2026" / "existing.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("summary https://youtube.com/watch?v=abcdefghijk", encoding="utf-8")
    ledger = mvi.read_ledger("/missing/manual.csv")
    ledger = mvi.upsert_ledger(ledger, video_id="missing0001", status="retryable_failed",
        reason="pending_drive: waiting", md_path=str(root / "2026" / "missing.md"))
    ledger = mvi.upsert_ledger(ledger, video_id="abcdefghijk", status="retryable_failed",
        reason="pending_drive: waiting", md_path=str(existing),
        md_sha256=__import__("hashlib").sha256(existing.read_text(encoding="utf-8").encode()).hexdigest())
    monkeypatch.setenv("P03_DRIVE_SYNC_ROOT", str(tmp_path / "drive"))
    calls = []
    class Pub:
        drive_ok = True
        drive_path = str(tmp_path / "drive" / "existing.md")
    Path(Pub.drive_path).parent.mkdir(parents=True, exist_ok=True)
    Path(Pub.drive_path).write_text(existing.read_text(encoding="utf-8"), encoding="utf-8")
    updated = mvi.retry_pending_drive(ledger, base_path=str(tmp_path), work_path=str(tmp_path / "work"),
        md_root=str(tmp_path / "md"), publisher=lambda **kw: calls.append(kw) or Pub())
    assert len(calls) == 1 and updated.loc[updated.video_id == "abcdefghijk", "status"].iloc[0] == "success"


def test_publisher_retains_manual_staging_until_readback(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts.drive_yt_summary import publish
    staged = tmp_path / "work" / "output_md_staging" / "2026" / "note.md"
    staged.parent.mkdir(parents=True)
    staged.write_text("expected", encoding="utf-8")
    destination = tmp_path / "drive" / "note.md"
    destination.parent.mkdir()
    destination.write_text("corrupted", encoding="utf-8")
    monkeypatch.setattr(publish, "load_config", lambda *a, **k: SimpleNamespace(
        enabled=True, sync_root=tmp_path / "drive", source_dir=tmp_path / "drive" / "source",
        state_path=tmp_path / "state.json"))
    monkeypatch.setattr(publish, "selective_mirror_enabled", lambda: True)
    monkeypatch.setattr(publish, "_classify_after_drive_write", lambda *a: None)
    class Adapter:
        def __init__(self, *_): pass
        def put(self, *a, **k): return "unchanged", str(destination), 1
    monkeypatch.setattr(publish, "DriveDesktopAdapter", Adapter)
    result = publish.publish_final_md(rel_path="2026/note.md", content="expected",
        staging_path=str(staged), md_root=str(tmp_path / "md"), mirror=False,
        base_path=str(tmp_path), work_path=str(tmp_path / "work"), video_id="abcdefghijk",
        retain_staging=True)
    assert result.drive_ok and staged.read_text(encoding="utf-8") == "expected"
    assert not mvi.verified_file_readback(result.drive_path, "expected")


def test_batch_flush_reconciles_manual_pending_drive(tmp_path):
    staged = tmp_path / "work" / "output_md_staging" / "2026" / "abcdefghijk.md"
    staged.parent.mkdir(parents=True)
    content = "summary for https://youtube.com/watch?v=abcdefghijk"
    staged.write_text(content, encoding="utf-8")
    ledger = mvi.upsert_ledger(mvi.read_ledger("/missing/manual.csv"), video_id="abcdefghijk",
                               status="retryable_failed", reason="pending_drive: waiting", md_path=str(staged),
                               md_sha256=__import__("hashlib").sha256(content.encode()).hexdigest())
    destination = tmp_path / "drive" / staged.name
    destination.parent.mkdir()
    destination.write_text(content, encoding="utf-8")
    class Published:
        drive_ok = True
        drive_path = str(destination)
    reconciled = mvi.reconcile_staging_flush(ledger, [Published()], {staged.name: content})
    assert reconciled.iloc[0].status == "success" and reconciled.iloc[0].drive_verified == "verified"
    assert not staged.exists()


def test_batch_flush_retains_only_manual_pending_staging_until_reconcile(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts.drive_yt_summary import publish
    root = tmp_path / "work" / "output_md_staging"
    manual = root / "2026" / "manual.md"
    channel = root / "2026" / "channel.md"
    manual.parent.mkdir(parents=True)
    manual.write_text("manual", encoding="utf-8")
    channel.write_text("channel", encoding="utf-8")
    calls = []
    def fake_publish(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(drive_ok=True, drive_action="unchanged",
                               drive_path=str(tmp_path / kwargs["rel_path"]))
    monkeypatch.setattr(publish, "publish_final_md", fake_publish)
    publish.flush_staging(md_root=str(tmp_path / "md"), base_path=str(tmp_path),
        work_path=str(tmp_path / "work"), retain_staging_paths={str(manual)})
    retain_by_path = {call["staging_path"]: call["retain_staging"] for call in calls}
    assert retain_by_path[str(manual)] is True
    assert retain_by_path[str(channel)] is False
    assert any(call["staging_path"] == str(manual) and call.get("force_rewrite") is True for call in calls)
    assert not any(call["staging_path"] == str(channel) and call.get("force_rewrite") for call in calls)


def test_missing_staging_is_reprocessed_even_if_destination_matches_hash(tmp_path, monkeypatch):
    root = tmp_path / "drive"
    (root / "source").mkdir(parents=True)
    content = "summary for https://youtube.com/watch?v=abcdefghijk"
    destination = root / "source" / "abcdefghijk.md"
    destination.write_text(content, encoding="utf-8")
    staged = tmp_path / "work" / "output_md_staging" / "2026" / destination.name
    ledger = mvi.upsert_ledger(mvi.read_ledger("/missing/manual.csv"), video_id="abcdefghijk",
        status="retryable_failed", reason="pending_drive: prior batch flush", md_path=str(staged),
        md_sha256=__import__("hashlib").sha256(content.encode("utf-8")).hexdigest())
    monkeypatch.setenv("P03_DRIVE_SYNC_ROOT", str(root))
    recovered = mvi.retry_pending_drive(ledger, base_path=str(tmp_path), work_path=str(tmp_path / "work"),
                                        md_root=str(tmp_path / "md"), publisher=lambda **_: pytest.fail("must not republish"))
    assert recovered.iloc[0].status == "retryable_failed"
    assert "reprocessing required" in recovered.iloc[0].reason
    queued, recovered = mvi.prepare_manual_queue(["https://youtu.be/abcdefghijk"], [],
        pd.DataFrame(), recovered, 5)
    assert [entry[0] for entry in queued] == ["abcdefghijk"]
    corrupted = destination.read_text(encoding="utf-8") + "corrupted"
    destination.write_text(corrupted, encoding="utf-8")
    pending = mvi.upsert_ledger(ledger, video_id="abcdefghijk", status="retryable_failed",
        reason="pending_drive: prior batch flush", md_path=str(staged))
    not_recovered = mvi.retry_pending_drive(pending, base_path=str(tmp_path), work_path=str(tmp_path / "work"),
        md_root=str(tmp_path / "md"), publisher=lambda **_: pytest.fail("must not republish"))
    assert not_recovered.iloc[0].status == "retryable_failed"


def test_manual_no_auto_caption_retry_is_scoped():
    import main
    assert main._is_terminal_video_status("NO_AUTO_CAPTION")
    assert main._is_terminal_video_status("NO_AUTO_CAPTION", manual_retry_no_auto_caption=False)
    assert not main._is_terminal_video_status("NO_AUTO_CAPTION", manual_retry_no_auto_caption=True)
    assert main._is_terminal_video_status("passed_shorts", manual_retry_no_auto_caption=True)


def test_manual_no_auto_caption_retries_operation_on_later_run(tmp_path, monkeypatch):
    import main
    calls = []
    monkeypatch.setattr(main, "_build_transcript_cache", lambda config: None)
    monkeypatch.setattr(main, "_find_exact_durable_full_transcript", lambda *_: None)
    monkeypatch.setattr(main.stt, "yt_downloader", lambda **kwargs: calls.append(kwargs) or
                        ("__NO_AUTO_CAPTION__", None, "abcdefghijk", 0, "", "", None))
    config = {"WORK_PATH": str(tmp_path), "USE_JOB_WORKSPACE": False,
              "manual_retry_no_auto_caption": False}
    args = ("https://youtu.be/abcdefghijk", config, None, None, pd.DataFrame(), str(tmp_path),
            str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path))
    first = main.process_single_video(*args)
    assert first.status == "NO_AUTO_CAPTION" and len(calls) == 1
    output = pd.DataFrame([{"v_id": "abcdefghijk", "status": first.status}])
    retry_args = list(args)
    retry_args[4] = output
    retry_config = {**config, "manual_retry_no_auto_caption": True}
    retry_args[1] = retry_config
    second = main.process_single_video(*retry_args)
    assert second.status == "NO_AUTO_CAPTION" and len(calls) == 2

