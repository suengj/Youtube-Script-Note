# -*- coding: utf-8 -*-
"""SUE-1297: channel_df obsidian column preservation + migration (synthetic data only)."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import channel_crawl as cc  # noqa: E402
from scripts import migrate_channel_df_obsidian as mig  # noqa: E402

CID1 = "UC" + "a" * 22
CID2 = "UC" + "b" * 22
UP1 = "UU" + "a" * 22
LEGACY = cc.CHANNEL_DF_COLUMNS[:-1]


def _write(dr, header, rows):
    with open(dr / "channel_df.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _read(dr):
    with open(dr / "channel_df.csv", encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def _legacy_rows():
    return [
        [f"https://www.youtube.com/channel/{CID1}", "n1", "u1", CID1, UP1, "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z", "1"],
        [f"https://www.youtube.com/channel/{CID2}", "n2", "u2", CID2, "", "", "", ""],
    ]


def test_legacy_load_save_roundtrip_no_obsidian_added(tmp_path):
    _write(tmp_path, LEGACY, _legacy_rows())
    rows = cc.load_channel_df(str(tmp_path))
    cc.save_channel_df(str(tmp_path), rows)
    out = _read(tmp_path)
    assert out[0] == LEGACY
    assert out[1] == _legacy_rows()[0]


@pytest.mark.parametrize("val,on", [("recording", True), ("  Recording ", True), ("", False), ("yes", False), ("recording2", False), (float("nan"), False), (None, False)])
def test_marker_policy(val, on):
    assert cc.is_obsidian_on(val) is on


def test_extra_columns_and_cursors_preserved(tmp_path):
    hdr = LEGACY + ["Obsidian", "note_x"]
    r = _legacy_rows()[0] + [" Recording", "keepme"]
    _write(tmp_path, hdr, [r])
    rows = cc.load_channel_df(str(tmp_path))
    assert rows[0]["obsidian"] == "Recording" and rows[0]["note_x"] == "keepme"
    rows[0]["last_processed_published_at"] = "2026-02-01T00:00:00Z"
    cc.save_channel_df(str(tmp_path), rows)
    out = _read(tmp_path)
    assert out[0] == LEGACY + ["obsidian", "note_x"]  # one obsidian column, canonical name
    assert out[1][3:5] == [CID1, UP1] and out[1][6] == "2026-01-03T00:00:00Z" and out[1][7] == "1"
    assert out[1][5] == "2026-02-01T00:00:00Z" and out[1][-1] == "keepme"


def test_new_channel_consumes_list_without_resetting_cursors(tmp_path):
    _write(tmp_path, LEGACY + ["obsidian"], [_legacy_rows()[0] + ["recording"]])
    rows = cc.load_channel_df(str(tmp_path))
    rows.append({"channel_url": f"https://www.youtube.com/channel/{CID2}", "channel_id": CID2, "obsidian": "recording"})
    cc.save_channel_df(str(tmp_path), rows)
    out = _read(tmp_path)
    assert out[1][5] == "2026-01-02T00:00:00Z" and out[1][6] == "2026-01-03T00:00:00Z"
    assert out[2][-1] == "recording" and out[2][5] == ""


def test_conflict_and_duplicate_header(tmp_path):
    _write(tmp_path, LEGACY + ["obsidian", "Obsidian"], [_legacy_rows()[0] + ["recording", "no"]])
    with pytest.raises(cc.ChannelDfSchemaError):
        cc.load_channel_df(str(tmp_path))
    _write(tmp_path, LEGACY + ["obsidian", "OBSIDIAN"], [_legacy_rows()[0] + ["recording", "Recording"]])
    h, rows = cc.read_channel_df_raw(str(tmp_path / "channel_df.csv"))
    assert h.count("obsidian") == 1 and rows[0]["obsidian"] == "recording"
    _write(tmp_path, LEGACY + ["channel_name"], [_legacy_rows()[0] + ["x"]])
    with pytest.raises(cc.ChannelDfSchemaError):
        cc.load_channel_df(str(tmp_path))


def test_migration_inspect_apply_twice(tmp_path):
    _write(tmp_path, LEGACY + ["Obsidian", "extra"], [_legacy_rows()[0] + ["", "e1"], _legacy_rows()[1] + ["recording", "e2"]])
    p = tmp_path / "channel_df.csv"
    before = p.read_bytes()
    assert mig.main(["inspect", "--data-root", str(tmp_path)]) == 0
    assert p.read_bytes() == before  # dry-run writes nothing
    assert mig.main(["apply", "--data-root", str(tmp_path)]) == 0
    out = _read(tmp_path)
    assert out[0] == LEGACY + ["obsidian", "extra"] and len(out) == 3
    assert out[2][-2:] == ["recording", "e2"]
    assert len(list(tmp_path.glob("channel_df.csv.bak_*"))) == 1
    after = p.read_bytes()
    assert mig.main(["apply", "--data-root", str(tmp_path)]) == 0  # no-op
    assert p.read_bytes() == after and len(list(tmp_path.glob("channel_df.csv.bak_*"))) == 1


def test_migration_legacy_adds_blank_column_preserving_rest(tmp_path):
    _write(tmp_path, LEGACY, _legacy_rows())
    assert mig.main(["apply", "--data-root", str(tmp_path)]) == 0
    out = _read(tmp_path)
    assert out[0] == LEGACY + ["obsidian"]
    assert [r[:-1] for r in out[1:]] == _legacy_rows() and all(r[-1] == "" for r in out[1:])


def test_migration_refuses_on_conflict_and_when_locked(tmp_path):
    _write(tmp_path, LEGACY + ["obsidian", "Obsidian"], [_legacy_rows()[0] + ["recording", "x"]])
    assert mig.main(["apply", "--data-root", str(tmp_path)]) == 3
    _write(tmp_path, LEGACY, _legacy_rows())
    import run_lock
    ok, h, _ = run_lock.acquire_run_lock(str(tmp_path), "scheduled", True)
    assert ok
    try:
        before = (tmp_path / "channel_df.csv").read_bytes()
        with pytest.raises(SystemExit):
            mig.main(["apply", "--data-root", str(tmp_path)])
        assert (tmp_path / "channel_df.csv").read_bytes() == before
    finally:
        h.release()


def test_queue_roundtrip_carries_obsidian_by_channel_id(tmp_path):
    q = cc.load_crawl_queue_df(str(tmp_path))
    assert "obsidian" in q.columns
    q = pd.DataFrame([{"video_id": "v1", "channel_id": CID1, "obsidian": "recording"},
                      {"video_id": "v2", "channel_id": "UCunknown", "obsidian": ""}])
    cc.save_crawl_queue_df(str(tmp_path), q)
    q2 = cc.load_crawl_queue_df(str(tmp_path))
    flags = {r.video_id: cc.is_obsidian_on(r.obsidian) for r in q2.itertuples()}
    assert flags == {"v1": True, "v2": False}  # blank read back as NaN -> OFF


def test_legacy_queue_without_column_defaults_off(tmp_path):
    cols = [c for c in cc.CRAWL_QUEUE_COLUMNS if c != "obsidian"]
    pd.DataFrame([{**{c: "" for c in cols}, "video_id": "v1"}]).to_csv(tmp_path / "crawl_yt_list.csv", index=False)
    q = cc.load_crawl_queue_df(str(tmp_path))
    assert not cc.is_obsidian_on(q.iloc[0]["obsidian"])
