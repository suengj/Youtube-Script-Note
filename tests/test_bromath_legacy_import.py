from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import channel_crawl as cc  # noqa: E402
import main  # noqa: E402
from scripts.bromath_legacy_import import import_reconciliation  # noqa: E402
from scripts.jev_classify.classify import UnresolvedSource, resolve_identity  # noqa: E402

BRO_ID = "UCHtzPPRYv5_GyszURLfzdKw"
BRO_UPLOADS = "UUHtzPPRYv5_GyszURLfzdKw"


def test_bromath_channel_csv_is_bom_header_and_requested_row():
    path = ROOT / "data" / "channel_df.csv"
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    match = [row for row in rows if row["channel_id"] == BRO_ID]
    assert len(match) == 1
    assert match[0] == {
        "channel_url": "https://www.youtube.com/@bromath_zero",
        "channel_name": "BroMath",
        "usage_channel": "BroMath",
        "channel_id": BRO_ID,
        "uploads_playlist_id": BRO_UPLOADS,
        "last_processed_published_at": "",
        "last_discovered_published_at": "",
        "auto_sub_only": "",
        "obsidian": "EMPTY",
    }


def _channel_root(path: Path, channels: list[dict[str, str]]) -> None:
    cc.save_channel_df(str(path), channels)


def test_empty_cursor_backfill_is_channel_scoped_and_still_filters_shorts(tmp_path, monkeypatch):
    monkeypatch.setenv("P03_CHANNEL_EMPTY_CURSOR_BACKFILL", "1")
    cid2, upload2 = "UC" + "b" * 22, "UU" + "b" * 22
    channels = [
        {"channel_url": "https://www.youtube.com/@bromath_zero", "channel_name": "BroMath",
         "usage_channel": "BroMath", "channel_id": BRO_ID, "uploads_playlist_id": BRO_UPLOADS,
         "last_processed_published_at": "", "last_discovered_published_at": "", "obsidian": "EMPTY"},
        {"channel_url": f"https://www.youtube.com/channel/{cid2}", "channel_name": "Other",
         "usage_channel": "Other", "channel_id": cid2, "uploads_playlist_id": upload2,
         "last_processed_published_at": "2024-01-02T00:00:00Z", "last_discovered_published_at": "2024-01-02T00:00:00Z", "obsidian": ""},
    ]
    _channel_root(tmp_path, channels)
    calls = []

    def fetch(_key, cid, uploads_id=None, cursor_dt=None):
        calls.append((cid, cursor_dt))
        entries = [{"video_id": "shortvid001", "url": "https://youtu.be/short_video1", "published_at": "2024-01-01T00:00:00Z"},
                   {"video_id": "regularvid1", "url": "https://youtu.be/regularvid1", "published_at": "2024-01-02T00:00:00Z"}]
        if cid != BRO_ID:
            entries = [{"video_id": "othervideo1", "url": "https://youtu.be/other_video1", "published_at": "2024-01-03T00:00:00Z"}]
        return entries, "", uploads_id or ""

    monkeypatch.setattr(cc, "fetch_channel_via_api", fetch)
    monkeypatch.setattr(cc, "_fetch_video_durations", lambda *_: ({"shortvid001": "PT60S", "regularvid1": "PT10M", "othervideo1": "PT10M"}, {}))
    queue, candidates, shorts = cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture", "FILTERING_SHORTS_MINUTES": 3},
        pd.DataFrame(columns=["v_id"]),
    )
    assert [cid for cid, _ in calls] == [BRO_ID, cid2]
    assert calls == [(BRO_ID, None), (cid2, cc._parse_iso_date("2024-01-02T00:00:00Z"))]
    assert set(queue["video_id"]) == {"shortvid001", "regularvid1", "othervideo1"}
    assert set(candidates["video_id"]) == {"regularvid1", "othervideo1"}
    assert [row["v_id"] for row in shorts] == ["shortvid001"]
    saved = cc.load_channel_df(str(tmp_path))
    assert all(row["last_discovered_published_at"] for row in saved)


def test_empty_cursor_backfill_flag_defaults_off(tmp_path, monkeypatch):
    monkeypatch.delenv("P03_CHANNEL_EMPTY_CURSOR_BACKFILL", raising=False)
    _channel_root(tmp_path, [{"channel_url": "https://www.youtube.com/@bromath_zero", "channel_name": "BroMath",
        "usage_channel": "BroMath", "channel_id": BRO_ID, "uploads_playlist_id": BRO_UPLOADS,
        "last_processed_published_at": "", "last_discovered_published_at": ""}])
    monkeypatch.setattr(cc, "fetch_channel_via_api", lambda *_args, **_kwargs: pytest.fail("must skip empty cursor"))
    _, candidates, _ = cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture"}, pd.DataFrame(columns=["v_id"])
    )
    assert candidates.empty


def test_durable_transcript_reuse_and_channel_extra_tags(tmp_path, monkeypatch):
    transcript = tmp_path / "title_videoid12345_full.txt"
    transcript.write_text("\ufeffdurable words", encoding="utf-8")
    monkeypatch.setattr(main, "find_durable_full_transcript", lambda root, vid: str(transcript) if vid == "videoid12345" else None)
    assert main._read_durable_full_transcript(str(tmp_path), "videoid12345") == (str(transcript), "durable words")
    assert main._read_durable_full_transcript(str(tmp_path), "differentid1") == (None, None)
    monkeypatch.setenv("P03_CHANNEL_EXTRA_TAGS", "BroMath:bromath;Other:finance")
    assert main._channel_extra_tags("BroMath") == ["bromath"]
    assert main._channel_extra_tags("other") == ["finance"]


def test_unknown_output_retries_only_with_unpublished_durable_transcript(monkeypatch):
    monkeypatch.setattr(main, "_has_drive_canonical_for_video", lambda *_: False)
    assert main._should_retry_unknown_with_transcript("unknown", "/full/a.txt", "abcdefghijk", {}, ".")
    assert not main._should_retry_unknown_with_transcript("success", "/full/a.txt", "abcdefghijk", {}, ".")
    monkeypatch.setattr(main, "_has_drive_canonical_for_video", lambda *_: True)
    assert not main._should_retry_unknown_with_transcript("unknown", "/full/a.txt", "abcdefghijk", {}, ".")
    assert not main._should_retry_unknown_with_transcript("unknown", None, "abcdefghijk", {}, ".")


def _item(root: Path, name: str, text: bytes, status: str, video_id: str = "") -> dict:
    file = root / name
    file.write_bytes(text)
    return {"stem": file.stem, "txt": name, "sha256_12": hashlib.sha256(text).hexdigest()[:12],
            "status": status, **({"video_id": video_id} if video_id else {})}


def test_legacy_import_hash_state_frontmatter_jev_and_dry_run(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    unmatched = _item(legacy, "unmatched.txt", b"\xef\xbb\xbfTranscript one", "LEGACY_UNMATCHED_IMPORT")
    matched = _item(legacy, "matched.txt", b"Transcript two", "LEGACY_MATCHED_IMPORT", "abcdefghijk")
    bad = dict(_item(legacy, "bad.txt", b"expected", "LEGACY_UNMATCHED_IMPORT"))
    bad["sha256_12"] = "0" * 12
    reconciliation = tmp_path / "reconciliation.json"
    reconciliation.write_text(json.dumps({"repo": "legacy", "commit": "abc", "items": [unmatched, matched, bad]}), encoding="utf-8")
    config = {"BASE_PATH": str(tmp_path / "base"), "WORK_PATH": str(tmp_path / "work"),
              "OUTPUT_MD_PATH": str(tmp_path / "vault"), "OPENAI_API_KEY": "fixture"}
    monkeypatch.setattr(main, "load_config", lambda: config)
    monkeypatch.setattr("scripts.bromath_legacy_import.OpenAI", lambda **_: object())
    summary_calls = []
    monkeypatch.setattr(main, "run_direct_summary", lambda client, text, filename, vid, cfg: (
        summary_calls.append((text, vid)) or (f"# {filename}\n\nSummary\n\n## Tags\n- custom\n", None)
    ))
    published = []

    class PublishResult:
        drive_ok = True

    def publish(**kwargs):
        published.append(kwargs)
        return PublishResult()

    monkeypatch.setattr("scripts.bromath_legacy_import.publish_final_md", publish)
    dry = import_reconciliation(reconciliation, legacy, dry_run=True)
    assert dry["stt_runs"] == dry["obsidian_writes"] == 0
    assert not summary_calls and not published

    counts = import_reconciliation(reconciliation, legacy)
    assert counts == {"imported": 2, "skipped_existing": 0, "skipped_hash_mismatch": 1,
                      "failed": 0, "stt_runs": 0, "obsidian_writes": 0}
    assert [call[1] for call in summary_calls] == ["", "abcdefghijk"]
    assert all(call["mirror"] is False for call in published)
    unmatched_md, matched_md = [call["content"] for call in published]
    assert "source: legacy_import" in unmatched_md and "tags:\n- bromath" in unmatched_md
    with pytest.raises(UnresolvedSource):
        resolve_identity(unmatched_md.encode())
    assert resolve_identity(matched_md.encode())[0] == "abcdefghijk"

    again = import_reconciliation(reconciliation, legacy)
    assert again["imported"] == 0 and again["skipped_existing"] == 2
    assert again["skipped_hash_mismatch"] == 1 and len(published) == 2
    assert all(p.read_bytes() in (b"\xef\xbb\xbfTranscript one", b"Transcript two", b"expected")
               for p in legacy.iterdir())
