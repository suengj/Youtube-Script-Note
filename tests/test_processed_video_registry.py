from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import processed_video_registry as pvr  # noqa: E402
from scripts import import_manual_urls_from_sheet as importer  # noqa: E402
from scripts.import_manual_urls_from_sheet import import_rows  # noqa: E402


def _empty_registry(root):
    pvr.write_registry(pd.DataFrame(columns=pvr.REGISTRY_COLUMNS), pvr.registry_path(root))


@pytest.mark.parametrize("source", sorted(pvr.REGISTRY_SOURCES))
def test_each_registry_source_marks_id_processed(tmp_path, source):
    pvr.merge_records(pvr.registry_path(tmp_path), [("abcdefghijk", source, "success", "model-x")])
    assert pvr.processed_ids(pvr.registry_path(tmp_path)) == {"abcdefghijk"}


def test_missing_registry_fails_closed_for_sheet_import_and_manual_queue(tmp_path, caplog):
    result = import_rows([["URL", "Source", "Download 여부"],
                          ["https://youtu.be/abcdefghijk", "", ""]],
                         tmp_path / "input_df.csv", tmp_path)
    assert result["new"] == 0
    assert not (tmp_path / "input_df.csv").exists()
    assert "fail-closed" in caplog.text
    assert pvr.processed_ids(pvr.registry_path(tmp_path)) is None
    import manual_video_input as mvi
    queued, _ = mvi.prepare_manual_queue(
        ["https://youtu.be/abcdefghijk"], [], pd.DataFrame(), mvi.read_ledger(tmp_path / "ledger.csv"))
    assert queued == []


def test_success_append_is_atomic_and_idempotent(tmp_path):
    _empty_registry(tmp_path)
    pvr.record_success(tmp_path, "abcdefghijk", source="channel_success", model="gpt-test")
    pvr.record_success(tmp_path, "abcdefghijk", source="channel_success", model="gpt-test")
    frame = pvr.read_registry(pvr.registry_path(tmp_path))
    assert len(frame) == 1
    assert frame.iloc[0].sources == "channel_success"
    assert frame.iloc[0].model == "gpt-test"


def test_rebuild_is_idempotent_and_never_removes_existing_ids(tmp_path):
    _empty_registry(tmp_path)
    pd.DataFrame([{"v_id": "unknown", "url": "https://youtu.be/outmatch001",
                   "status": "download_failed", "model": ""}]).to_csv(
                       tmp_path / "output_df_new.csv", index=False)
    pd.DataFrame([{"video_id": "ledger00001", "status": "skipped_historical",
                   "first_seen_at": "2026-01-01T00:00:00+00:00", "model": ""}]).to_csv(
                       tmp_path / "manual_input_status.csv", index=False)
    sheet_rows = [["URL", "Source", "Download 여부"],
                  ["https://youtu.be/sheet000001", "owner", ""],
                  ["https://youtu.be/blank000001", "", ""]]
    pvr.build_registry(tmp_path, include_sheet=True, sheet_rows=sheet_rows)
    first = pvr.read_registry(pvr.registry_path(tmp_path)).to_dict("records")
    pvr.build_registry(tmp_path, include_sheet=True, sheet_rows=sheet_rows)
    second = pvr.read_registry(pvr.registry_path(tmp_path)).to_dict("records")
    assert first == second
    assert {row["video_id"] for row in first} == {"outmatch001", "ledger00001", "sheet000001"}
    assert "output_df" in next(row["sources"] for row in first if row["video_id"] == "outmatch001")
    assert "skipped_historical" in next(row["sources"] for row in first if row["video_id"] == "ledger00001")
    assert "sheet_marked" in next(row["sources"] for row in first if row["video_id"] == "sheet000001")


@pytest.mark.parametrize(("source", "download"), [("owner", ""), ("", "done")])
def test_sheet_source_or_download_marker_is_processed(tmp_path, source, download):
    _empty_registry(tmp_path)
    rows = [["URL", "Source", "Download 여부"],
            ["https://youtu.be/marked00001", source, download],
            ["https://youtu.be/unmarked001", "", ""]]
    pvr.build_registry(tmp_path, include_sheet=True, sheet_rows=rows)
    assert pvr.processed_ids(pvr.registry_path(tmp_path)) == {"marked00001"}


def test_importer_treats_sheet_marked_only_id_as_processed_even_on_duplicate_row(tmp_path):
    _empty_registry(tmp_path)
    rows = [["URL", "Source", "Download 여부"],
            ["https://youtu.be/marked00001", "owner", ""],
            ["https://www.youtube.com/watch?v=marked00001&si=dup", "", ""],
            ["https://youtu.be/freshvid001", "", ""]]

    result = import_rows(rows, tmp_path / "input_df.csv", tmp_path)

    assert result["new"] == 1
    assert result["already_processed"] == 2
    record = pvr.read_registry(pvr.registry_path(tmp_path)).set_index("video_id").loc["marked00001"]
    assert record.sources == "sheet_marked"
    assert record.last_status == "sheet_marked"


def test_disagreement_sheet_marked_repairs_registry_source(tmp_path):
    _empty_registry(tmp_path)
    rows = [["URL", "Source", "Download 여부"],
            ["https://youtu.be/repair00001", "", "processed"],
            ["https://youtu.be/repair00001", "", ""]]

    result = import_rows(rows, tmp_path / "input_df.csv", tmp_path, dry_run=True)

    assert result["already_processed"] == 2
    record = pvr.read_registry(pvr.registry_path(tmp_path)).set_index("video_id").loc["repair00001"]
    assert record.sources == "sheet_marked"
    assert record.last_status == "sheet_marked"


def test_manual_queue_excludes_live_sheet_marked_only_video(tmp_path):
    import manual_video_input as mvi

    _empty_registry(tmp_path)
    rows = [["URL", "Source", "Download 여부"],
            ["https://youtu.be/marked00001", "", "done"]]
    sheet_ids = pvr.sheet_marked_ids(rows, mvi.normalize_youtube_url)
    processed_ids = pvr.reconcile_sheet_marked(pvr.registry_path(tmp_path), sheet_ids)

    queued, _ = mvi.prepare_manual_queue(
        ["https://youtu.be/marked00001"], [], pd.DataFrame(), mvi.read_ledger(tmp_path / "ledger.csv"),
        processed_video_ids=processed_ids)

    assert queued == []


def test_unreadable_sheet_import_is_fail_closed_with_registry_only_state(tmp_path, monkeypatch, caplog):
    _empty_registry(tmp_path)
    pvr.merge_records(pvr.registry_path(tmp_path), [("existing0001", "output_df", "success", "")])
    monkeypatch.setattr(importer, "_read_sheet_rows", lambda *_args: (_ for _ in ()).throw(OSError("offline")))

    result = importer.import_from_sheet(tmp_path, since="2026-01-01")

    assert result["new"] == 0
    assert pvr.processed_ids(pvr.registry_path(tmp_path)) == {"existing0001"}
    assert not (tmp_path / "input_df.csv").exists()
    assert "registry-only processed checks" in caplog.text


def test_manual_success_ledger_row_is_processed(tmp_path):
    _empty_registry(tmp_path)
    pd.DataFrame([{"video_id": "manual00001", "status": "success", "model": "gpt-test"},
                  {"video_id": "pending0001", "status": "retryable_failed"}]).to_csv(
                      tmp_path / "manual_input_status.csv", index=False)
    pvr.build_registry(tmp_path, include_sheet=False)
    frame = pvr.read_registry(pvr.registry_path(tmp_path))
    assert pvr.processed_ids(pvr.registry_path(tmp_path)) == {"manual00001"}
    assert frame.iloc[0].model == "gpt-test"


def test_manual_queue_uses_registry_as_processed_authority():
    import manual_video_input as mvi
    queued, _ = mvi.prepare_manual_queue(
        ["https://youtu.be/abcdefghijk"], [],
        pd.DataFrame([{"v_id": "abcdefghijk", "status": "download_failed"}]),
        mvi.read_ledger("/missing/ledger.csv"), processed_video_ids={"abcdefghijk"})
    assert queued == []
    queued, _ = mvi.prepare_manual_queue(
        ["https://youtu.be/abcdefghijk"], [],
        pd.DataFrame([{"v_id": "abcdefghijk", "status": "success"}]),
        mvi.read_ledger("/missing/ledger.csv"), processed_video_ids=set())
    assert [item[0] for item in queued] == ["abcdefghijk"]
