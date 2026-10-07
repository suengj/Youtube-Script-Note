from __future__ import annotations

import csv
import hashlib
import json
from types import SimpleNamespace
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



def _channel_root(path: Path, channels: list[dict[str, str]]) -> None:
    cc.save_channel_df(str(path), channels)


def test_empty_cursor_backfill_is_channel_scoped_and_still_filters_shorts(tmp_path, monkeypatch):
    monkeypatch.setenv("P03_CHANNEL_EMPTY_CURSOR_BACKFILL", BRO_ID)
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


def test_empty_cursor_backfill_skips_unlisted_channel(tmp_path, monkeypatch):
    selected, unlisted = BRO_ID, "UC" + "c" * 22
    monkeypatch.setenv("P03_CHANNEL_EMPTY_CURSOR_BACKFILL", f" {selected} ")
    _channel_root(tmp_path, [
        {"channel_url": "https://www.youtube.com/@selected", "channel_name": "Selected",
         "channel_id": selected, "uploads_playlist_id": BRO_UPLOADS,
         "last_processed_published_at": "", "last_discovered_published_at": ""},
        {"channel_url": f"https://www.youtube.com/channel/{unlisted}", "channel_name": "Unlisted",
         "channel_id": unlisted, "uploads_playlist_id": "UU" + "c" * 22,
         "last_processed_published_at": "", "last_discovered_published_at": ""},
    ])
    calls = []
    monkeypatch.setattr(cc, "fetch_channel_via_api", lambda _key, cid, **kwargs: (
        calls.append((cid, kwargs.get("cursor_dt"))) or ([], "", kwargs.get("uploads_id") or "")
    ))
    cc.build_queue_and_get_candidates(
        str(tmp_path), {"YOUTUBE_API_KEY": "fixture"}, pd.DataFrame(columns=["v_id"])
    )
    assert calls == [(selected, None)]


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
    transcript = tmp_path / "title+vid-abcdefghijk.txt"
    transcript.write_text("\ufeffdurable words", encoding="utf-8")
    assert main._read_durable_full_transcript(str(tmp_path), "abcdefghijk") == (str(transcript), "durable words")
    assert main._read_durable_full_transcript(str(tmp_path), "differentid1") == (None, None)
    monkeypatch.setenv("P03_CHANNEL_EXTRA_TAGS", "BroMath:bromath;Other:finance")
    assert main._channel_extra_tags("BroMath") == ["bromath"]
    assert main._channel_extra_tags("other") == ["finance"]


def test_durable_transcript_reuse_requires_exact_parsed_video_id(tmp_path):
    wrong = tmp_path / "title_prefixabcdefghijk_full.txt"
    wrong.write_text("wrong video transcript", encoding="utf-8")
    overlong = tmp_path / "title+vid-abcdefghijkextra.txt"
    overlong.write_text("wrong video transcript", encoding="utf-8")
    assert main._find_exact_durable_full_transcript(str(tmp_path), "abcdefghijk") is None

    exact = tmp_path / "title.m4a+vid-abcdefghijk_full.txt"
    exact.write_text("exact video transcript", encoding="utf-8")
    assert main._find_exact_durable_full_transcript(str(tmp_path), "abcdefghijk") == str(exact)


def test_unreadable_durable_transcript_is_reported_as_present(tmp_path, monkeypatch):
    transcript = tmp_path / "title+vid-abcdefghijk.txt"
    transcript.write_bytes(b"valid prefix \xff invalid utf8")
    monkeypatch.setattr(main, "_find_exact_durable_full_transcript", lambda *_: str(transcript))
    path, text = main._read_durable_full_transcript(str(tmp_path), "abcdefghijk")
    assert path == str(transcript) and text is None

    monkeypatch.setattr(main.stt, "extract_youtube_id", lambda _url: "abcdefghijk")
    monkeypatch.setattr(main.stt, "yt_downloader", lambda **_kwargs: pytest.fail("must not download when durable transcript is unreadable"))
    config = {"WORK_PATH": str(tmp_path), "USE_JOB_WORKSPACE": False, "TRANSCRIPT_CACHE_ENABLED": False}
    result = main.process_single_video(
        "https://www.youtube.com/watch?v=abcdefghijk", config, object(), object(),
        pd.DataFrame(columns=["v_id", "status"]), str(tmp_path), str(tmp_path),
        str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path),
    )
    assert result.status == "durable_transcript_unreadable"


def test_unknown_rerun_requires_exact_drive_video_identity(tmp_path, monkeypatch):
    from scripts.drive_yt_summary import config as drive_config

    source = tmp_path / "drive" / "source"
    source.mkdir(parents=True)
    note = source / "old_date_canonical.md"
    note.write_text("---\nvid: prefixabcdefghijksuffix\n---\nbody\n", encoding="utf-8")
    fake = SimpleNamespace(
        state_path=tmp_path / "state.json", source_dir=source, sync_root=source.parent,
    )
    monkeypatch.setattr(drive_config, "load_config", lambda *_args, **_kwargs: fake)
    assert not main._has_drive_canonical_for_video("abcdefghijk", {}, str(tmp_path))
    note.write_text("---\nvid: abcdefghijk\n---\nbody\n", encoding="utf-8")
    assert main._has_drive_canonical_for_video("abcdefghijk", {}, str(tmp_path))
    assert main._drive_canonical_relative_path("abcdefghijk", {}, str(tmp_path)) == "canonical/old_date_canonical.md"
    note.unlink()
    exact_name = source / "channel_title_abcdefghijk_ko_5-mini.md"
    exact_name.write_text("body\n", encoding="utf-8")
    assert main._has_drive_canonical_for_video("abcdefghijk", {}, str(tmp_path))
    exact_name.unlink()
    substring_name = source / "channel_title_prefixabcdefghijk_ko_5-mini.md"
    substring_name.write_text("body\n", encoding="utf-8")
    assert not main._has_drive_canonical_for_video("abcdefghijk", {}, str(tmp_path))


def test_processed_video_dedupes_before_reading_damaged_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(main.stt, "extract_youtube_id", lambda _url: "abcdefghijk")
    monkeypatch.setattr(main, "_find_exact_durable_full_transcript", lambda *_: str(tmp_path / "damaged.txt"))
    monkeypatch.setattr(main, "_read_durable_full_transcript", lambda *_: pytest.fail("must dedupe before transcript read"))
    transcript = tmp_path / "damaged.txt"
    transcript.write_bytes(b"old transcript \xff")
    config = {"WORK_PATH": str(tmp_path), "USE_JOB_WORKSPACE": False, "TRANSCRIPT_CACHE_ENABLED": False}
    result = main.process_single_video(
        "https://www.youtube.com/watch?v=abcdefghijk", config, object(), object(),
        pd.DataFrame([{"v_id": "abcdefghijk", "status": "success"}]),
        str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path),
        str(tmp_path), str(tmp_path),
    )
    assert result.status == "already_existed" and result.stage == "dedupe"


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
    assert all(call["never_mirror"] is True for call in published)
    assert unmatched_md.split("---", 2)[1].count("source:") == 1
    assert matched_md.split("---", 2)[1].count("source:") == 1
    assert "source: legacy_import" in unmatched_md and "tags:\n- bromath" in unmatched_md
    with pytest.raises(UnresolvedSource):
        resolve_identity(unmatched_md.encode())
    assert resolve_identity(matched_md.encode())[0] == "abcdefghijk"

    again = import_reconciliation(reconciliation, legacy)
    assert again["imported"] == 0 and again["skipped_existing"] == 2
    assert again["skipped_hash_mismatch"] == 1 and len(published) == 2
    assert all(p.read_bytes() in (b"\xef\xbb\xbfTranscript one", b"Transcript two", b"expected")
               for p in legacy.iterdir())
