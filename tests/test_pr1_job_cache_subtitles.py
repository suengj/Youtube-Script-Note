#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR1: job workspace, transcript cache, subtitle lifecycle."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import stt_function_v3 as stt
from job_workspace import VideoJobWorkspace, cleanup_stale_jobs
from transcript_cache import TranscriptCache, find_durable_full_transcript, should_write_transcript_cache
from subtitle_lifecycle import (
    delete_subtitle_file,
    inventory_yt_subs,
    move_to_quarantine,
    quarantine_job_subtitles,
)

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


@pytest.fixture
def tmp_work(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return str(work)


class TestSubtitleParse:
    def test_vtt_parse_ok(self):
        text = stt.subtitle_file_to_plain_text(str(FIXTURES / "sample_ok.vtt"))
        assert "Hello world" in text
        assert "Second line" in text

    def test_vtt_rolling_dedup(self):
        text = stt.subtitle_file_to_plain_text(str(FIXTURES / "sample_rolling.vtt"))
        assert "Hello world today" in text
        assert text.count("Hello") == 1

    def test_srt_parse(self):
        text = stt.subtitle_file_to_plain_text(str(FIXTURES / "sample_ok.srt"))
        assert "SRT hello" in text
        assert "SRT world" in text

    def test_incomplete_vtt(self):
        assert stt._subtitle_file_is_incomplete(str(FIXTURES / "sample_truncated.vtt"), 120.0)


class TestJobWorkspace:
    def test_isolated_paths(self, tmp_work):
        w1 = VideoJobWorkspace(tmp_work, "vidA")
        w2 = VideoJobWorkspace(tmp_work, "vidB")
        w1.ensure()
        w2.ensure()
        assert w1.output_wav_path() != w2.output_wav_path()
        p1 = w1.output_wav_path()
        p2 = w2.output_wav_path()
        Path(p1).write_text("wav1")
        Path(p2).write_text("wav2")
        assert Path(p1).read_text() == "wav1"
        assert Path(p2).read_text() == "wav2"

    def test_subtitle_delete_after_parse(self, tmp_work):
        ws = VideoJobWorkspace(tmp_work, "abc123")
        ws.ensure()
        sub = os.path.join(ws.root, "abc123.en.vtt")
        shutil_copy = FIXTURES / "sample_ok.vtt"
        import shutil
        shutil.copy(shutil_copy, sub)
        text = stt.subtitle_file_to_plain_text(sub)
        assert text
        ws.remove_subtitle_files()
        assert not os.path.isfile(sub)

    def test_cleanup_preserves_active(self, tmp_work):
        ws = VideoJobWorkspace(tmp_work, "active")
        ws.ensure()
        old = VideoJobWorkspace(tmp_work, "stale")
        old.ensure()
        stale_dir = old.root
        os.utime(stale_dir, (time.time() - 7200, time.time() - 7200))
        removed, skipped = cleanup_stale_jobs(tmp_work, max_age_sec=3600)
        assert removed >= 1
        assert os.path.isdir(ws.root)


class TestTranscriptCache:
    def test_atomic_put_get(self, tmp_work):
        cache = TranscriptCache(os.path.join(tmp_work, "cache"), enabled=True, ttl_hours=72)
        cache.put("vid1", "plain text here", transcript_source="auto_subs")
        entry = cache.get("vid1")
        assert entry is not None
        assert entry["text"] == "plain text here"
        assert entry["video_id"] == "vid1"

    def test_expired_cache(self, tmp_work):
        cache = TranscriptCache(os.path.join(tmp_work, "cache"), enabled=True, ttl_hours=0)
        cache.put("vid2", "old text", transcript_source="subs")
        path = cache._path("vid2")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["expires_at"] = "2020-01-01T00:00:00Z"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        assert cache.get("vid2") is None

    def test_skip_cache_when_durable_full(self, tmp_work):
        full_dir = tmp_work + "/full"
        os.makedirs(full_dir, exist_ok=True)
        durable = os.path.join(full_dir, "title_vid-XYZ123.txt")
        with open(durable, "w") as f:
            f.write("existing")
        found = find_durable_full_transcript(full_dir, "XYZ123")
        assert found == durable
        assert not should_write_transcript_cache(
            enabled=True,
            durable_full_path=found,
            subs_source="auto",
            save_full_when_auto_subs=False,
        )
        assert should_write_transcript_cache(
            enabled=True,
            durable_full_path=None,
            subs_source="auto",
            save_full_when_auto_subs=False,
        )

    def test_broken_json(self, tmp_work):
        cache = TranscriptCache(os.path.join(tmp_work, "cache"), enabled=True)
        os.makedirs(cache.transcripts_dir, exist_ok=True)
        bad = os.path.join(cache.transcripts_dir, "bad.json")
        with open(bad, "w") as f:
            f.write("{not json")
        assert cache.get("bad") is None


class TestQuarantine:
    def test_quarantine_on_failure(self, tmp_work):
        ws = VideoJobWorkspace(tmp_work, "failvid")
        ws.ensure()
        sub = os.path.join(ws.root, "failvid.ko.vtt")
        import shutil
        shutil.copy(FIXTURES / "sample_truncated.vtt", sub)
        n = quarantine_job_subtitles(tmp_work, "failvid", [sub], reason="parse_fail")
        assert n == 1
        assert not os.path.isfile(sub)
        qdir = os.path.join(tmp_work, "quarantine", "subtitles", "failvid")
        assert os.path.isdir(qdir)

    def test_delete_subtitle(self, tmp_work):
        p = os.path.join(tmp_work, "x.vtt")
        with open(p, "w") as f:
            f.write("WEBVTT\n")
        assert delete_subtitle_file(p)
        assert not os.path.isfile(p)


class TestLegacyInventory:
    def test_inventory(self, tmp_work):
        d = os.path.join(tmp_work, "yt_subs")
        os.makedirs(d)
        with open(os.path.join(d, "a.en.vtt"), "w") as f:
            f.write("WEBVTT\n\n")
        c, b = inventory_yt_subs(d)
        assert c == 1
        assert b > 0
