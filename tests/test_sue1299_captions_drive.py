"""Offline regression checks for the SUE-1299 Linux/API boundary."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from drive_api_sink import DriveApiSink, DriveUploadError, MANIFEST_FILE_ID, SOURCE_FOLDER_ID, SUMMARY_FOLDER_ID

VIDEO_ID = "abcdefghijk"


class _Request:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class _FakeDrive:
    def __init__(self, mismatch=False, mismatch_name="", bad_readback=""):
        import hashlib
        self.items = {}
        self.n = 1
        self.update_calls = []
        self.mismatch = mismatch
        self.mismatch_name = mismatch_name
        self.bad_readback = bad_readback
        initial = b"version: 1\nitems: []\n"
        self.items[MANIFEST_FILE_ID] = {
            "id": MANIFEST_FILE_ID, "name": "manifest.yaml", "mimeType": "text/yaml",
            "parents": [SUMMARY_FOLDER_ID], "data": initial, "size": str(len(initial)),
            "md5Checksum": hashlib.md5(initial).hexdigest(), "headRevisionId": "1",
        }
    def files(self): return self
    def list(self, q="", **kwargs):
        def run():
            if "mimeType='application/vnd.google-apps.folder'" in q:
                return {"files": [v for v in self.items.values() if v.get("name") == "transcripts" and v.get("mimeType") == "application/vnd.google-apps.folder"]}
            parent = q.split("'", 2)[1] if "'" in q else ""
            name = q.split("name='", 1)[1].split("'", 1)[0] if "name='" in q else None
            return {"files": [v for v in self.items.values() if parent in v.get("parents", []) and (name is None or v.get("name") == name) and not v.get("trashed")]}
        return _Request(run)
    def create(self, body, media_body=None, **kwargs):
        def run():
            self.n += 1; fid = str(self.n)
            data = getattr(media_body, "data", b"")
            bad = self.mismatch or body.get("name") == self.mismatch_name
            self.items[fid] = {**body, "id": fid, "size": str(len(data)), "md5Checksum": "bad" if bad else __import__("hashlib").md5(data).hexdigest(), "data": data}
            if body.get("mimeType") == "application/vnd.google-apps.folder": self.items[fid]["size"] = "0"
            return {"id": fid}
        return _Request(run)
    def update(self, fileId, body=None, media_body=None, **kwargs):
        def run():
            import hashlib
            item = self.items[fileId]; item.update(body or {})
            if media_body is not None:
                data = getattr(media_body, "data", b""); item["data"] = data; item["size"] = str(len(data))
                bad = self.mismatch or item.get("name") == self.mismatch_name
                item["md5Checksum"] = "bad" if bad else __import__("hashlib").md5(data).hexdigest()
                if fileId == MANIFEST_FILE_ID:
                    item["headRevisionId"] = str(int(item.get("headRevisionId", "0")) + 1)
                    self.update_calls.append(fileId)
            return {"id": fileId}
        return _Request(run)
    def get(self, fileId, **kwargs):
        def run():
            meta = dict(self.items[fileId]); meta["parents"] = list(meta.get("parents", [])); meta["appProperties"] = dict(meta.get("appProperties", {}))
            if self.bad_readback == "id": meta["id"] = "wrong"
            if self.bad_readback == "parents": meta["parents"] = ["wrong"]
            if self.bad_readback == "size": meta["size"] = "999"
            if self.bad_readback == "md5Checksum": meta["md5Checksum"] = "bad"
            if self.bad_readback == "appProperties": meta["appProperties"]["p03_input_revision"] = "wrong"
            if self.bad_readback == "manifest_md5" and fileId == MANIFEST_FILE_ID: meta["md5Checksum"] = "bad"
            return meta
        return _Request(run)
    def get_media(self, fileId):
        return _Request(lambda: self.items[fileId].get("data", b""))


def _install_fake_media():
    google = types.ModuleType("googleapiclient")
    http = types.ModuleType("googleapiclient.http")
    class Media:
        def __init__(self, stream, **kwargs): self.data = stream.read()
    http.MediaIoBaseUpload = Media
    sys.modules["googleapiclient"] = google
    sys.modules["googleapiclient.http"] = http


class Sue1299DriveSinkTests(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    _install_fake_media()

  def test_drive_sink_creates_then_updates_same_identity(self):
    drive = _FakeDrive()
    sink = DriveApiSink(drive)
    args = dict(summary_name=f"Channel_title_{VIDEO_ID}_en-orig_auto_subs_5-mini.md",
                summary=b"summary", transcript=b"# title\ntext", metadata={"provider": "fake"}, video_id=VIDEO_ID)
    first = sink.upload_video(**args)
    second = sink.upload_video(**{**args, "summary_name": f"Channel_renamed_{VIDEO_ID}_en-orig_auto_subs_5-mini.md", "summary": b"summary v2"})
    self.assertEqual(first["summary"]["parents"], [SOURCE_FOLDER_ID])
    self.assertEqual(second["summary"]["id"], first["summary"]["id"])
    self.assertEqual(len([x for x in drive.items.values() if x.get("appProperties", {}).get("p03_kind") == "summary"]), 1)
    self.assertEqual(second["summary"]["appProperties"]["p03_video_id"], VIDEO_ID)
    self.assertEqual(second["summary"]["appProperties"]["p03_kind"], "summary")
    self.assertTrue(sink.already_uploaded(video_id=VIDEO_ID, transcript=b"# title\ntext"))
    self.assertFalse(sink.already_uploaded(video_id=VIDEO_ID, transcript=b"different revision"))

  def test_dedupe_rejects_tagged_artifact_with_corrupt_bytes(self):
    drive = _FakeDrive()
    sink = DriveApiSink(drive)
    sink.upload_video(summary_name=f"title_{VIDEO_ID}.md", summary=b"summary", transcript=b"caption", metadata={}, video_id=VIDEO_ID)
    summary = next(item for item in drive.items.values() if item.get("appProperties", {}).get("p03_kind") == "summary")
    summary["data"] = b"mutated bytes"
    self.assertFalse(sink.already_uploaded(video_id=VIDEO_ID, transcript=b"caption"))

  def test_dedupe_checks_all_three_inner_records_and_revision(self):
    drive = _FakeDrive()
    sink = DriveApiSink(drive)
    sink.upload_video(summary_name=f"title_{VIDEO_ID}.md", summary=b"summary", transcript=b"caption", metadata={}, video_id=VIDEO_ID)
    for kind in ("summary", "transcript", "metadata"):
      with self.subTest(kind=kind):
        item = next(item for item in drive.items.values() if item.get("appProperties", {}).get("p03_kind") == kind)
        item["appProperties"]["p03_input_revision"] = "wrong"
        self.assertFalse(sink.already_uploaded(video_id=VIDEO_ID, transcript=b"caption"))
        item["appProperties"]["p03_input_revision"] = __import__("hashlib").sha256(b"caption").hexdigest()


  def test_source_identity_and_path_substitution_fail_closed(self):
    sink = DriveApiSink(service=object())
    with self.assertRaises(DriveUploadError):
        sink.upload_video(summary_name=f"title_{VIDEO_ID}.md", summary=b"", transcript=b"", metadata={}, video_id="caller-other")
    with self.assertRaises(DriveUploadError):
        sink.upload_video(summary_name=f"../title_{VIDEO_ID}.md", summary=b"", transcript=b"", metadata={}, video_id=VIDEO_ID)

  def test_symlink_payload_is_rejected_without_following_local_paths(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      tmp_path = Path(temp_dir)
      target = tmp_path / "target.txt"
      target.write_text("synthetic only", encoding="utf-8")
      link = tmp_path / "payload.md"
      link.symlink_to(target)
      with self.assertRaises(DriveUploadError):
        DriveApiSink(service=object()).upload_video(
          summary_name=f"title_{VIDEO_ID}.md", summary=link, transcript=b"t", metadata={}, video_id=VIDEO_ID
        )


  def test_nested_readback_mismatch_does_not_return_success(self):
    sink = DriveApiSink(_FakeDrive(mismatch=True))
    with self.assertRaisesRegex(DriveUploadError, "readback"):
        sink.upload_video(summary_name=f"title_{VIDEO_ID}.md", summary=b"summary", transcript=b"transcript", metadata={}, video_id=VIDEO_ID)

  def test_partial_artifact_failure_does_not_return_done_result(self):
    sink = DriveApiSink(_FakeDrive(mismatch_name=f"title_{VIDEO_ID}.transcript.md"))
    with self.assertRaisesRegex(DriveUploadError, "readback"):
      sink.upload_video(summary_name=f"title_{VIDEO_ID}.md", summary=b"summary", transcript=b"transcript", metadata={}, video_id=VIDEO_ID)

  def test_every_inner_readback_identity_field_is_checked(self):
    with patch("drive_api_sink.time.sleep", return_value=None):
      for field in ("id", "parents", "size", "md5Checksum", "appProperties"):
        with self.subTest(field=field), self.assertRaises(DriveUploadError):
          sink = DriveApiSink(_FakeDrive(bad_readback=field))
          sink.upload_video(summary_name=f"title_{VIDEO_ID}.md", summary=b"summary", transcript=b"transcript", metadata={}, video_id=VIDEO_ID)


  def test_missing_malformed_and_oversized_payloads_are_typed(self):
    cases = [
      {"summary_name": "", "summary": b"x", "transcript": b"t", "video_id": VIDEO_ID},
      {"summary_name": f"title_{VIDEO_ID}.md", "summary": "not bytes", "transcript": b"t", "video_id": VIDEO_ID},
      {"summary_name": f"title_{VIDEO_ID}.md", "summary": b"x", "transcript": b"t", "video_id": VIDEO_ID, "metadata": None},
      {"summary_name": f"title_{VIDEO_ID}.md", "summary": type("OversizedBytes", (bytes,), {"__len__": lambda self: 128 * 1024 * 1024 + 1})(b"x"), "transcript": b"t", "video_id": VIDEO_ID},
    ]
    for kwargs in cases:
      with self.subTest(kwargs=kwargs), self.assertRaises(DriveUploadError):
        DriveApiSink(service=object()).upload_video(metadata=kwargs.pop("metadata", {}), **kwargs)


  def test_caption_only_contract_and_refusal_status_are_explicit(self):
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    stt = (ROOT / "stt_function_v3.py").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    crawl = (ROOT / "channel_crawl.py").read_text(encoding="utf-8")
    self.assertIn('return _vr("NO_AUTO_CAPTION"', main)
    self.assertIn('"SOURCE_UNAVAILABLE"', main)
    self.assertIn('refusal = re.sub', main)
    retryable = main[main.index("_retryable_statuses = frozenset("):main.index("if 'v_id' in output_df.columns", main.index("_retryable_statuses = frozenset("))]
    self.assertIn('"download_failed"', retryable)
    self.assertIn('"captions_error"', retryable)
    downloader = stt[stt.index("def yt_downloader("):stt.index("def yt_downloader_ytdlp(")]
    self.assertNotIn("yt_downloader_pytubefix", downloader)
    ytdlp_downloader = stt[stt.index("def yt_downloader_ytdlp("):stt.index("def _is_errno11_deadlock(")]
    self.assertNotIn("ydl.download([URL])", ytdlp_downloader)
    self.assertIn("__NO_AUTO_CAPTION__", stt)
    self.assertNotIn("mlx_whisper", stt)
    self.assertNotIn("transcribe_by_groq", stt)
    self.assertNotIn("convert_to_wav", stt)
    self.assertNotIn("mlx-whisper", requirements)
    self.assertNotIn("moviepy", requirements)
    self.assertIn('if not scheduled_env and sys.platform == "darwin"', main)
    self.assertIn("SYSTEMD_SCHEDULED", main)
    self.assertIn("source_unavailable", crawl)
    self.assertIn("captions_error", crawl)
    service = (ROOT / "deploy/systemd/p03-batch.service").read_text(encoding="utf-8")
    self.assertIn("MemoryHigh=1G", service)
    self.assertIn("MemoryMax=1536M", service)
    self.assertIn("CPUQuota=100%", service)
    self.assertIn("Nice=15", service)
    self.assertIn("IOWeight=20", service)
    self.assertIn("TimeoutStartSec=2h", service)

  def test_metadata_extract_requests_captions_without_media_format(self):
    import stt_function_v3 as stt

    captured = {}
    class FakeYoutubeDL:
      def __init__(self, options):
        captured.update(options)
      def __enter__(self):
        return self
      def __exit__(self, *args):
        return False
      def extract_info(self, url, download=False):
        self.download = download
        return {"id": VIDEO_ID, "automatic_captions": {}}

    with tempfile.TemporaryDirectory() as temp_dir, patch.object(stt.yt_dlp, "YoutubeDL", FakeYoutubeDL):
      result = stt.yt_downloader_ytdlp(
        f"https://www.youtube.com/watch?v={VIDEO_ID}", temp_dir, VIDEO_ID,
        max_retries=1, config={"BASE_PATH": temp_dir, "WORK_PATH": temp_dir, "JOB_SUBS_DIR": temp_dir},
      )

    self.assertEqual(result[0], "__NO_AUTO_CAPTION__")
    self.assertTrue(captured["skip_download"])
    self.assertTrue(captured["ignore_no_formats_error"])
    for forbidden in ("format", "postprocessors", "extractor_args", "outtmpl", "keepvideo", "progress_hooks"):
      self.assertNotIn(forbidden, captured)

  def test_existing_caption_download_failure_is_retryable_not_no_caption(self):
    import stt_function_v3 as stt
    captured = {}
    class FakeYoutubeDL:
      def __init__(self, options): captured.update(options)
      def __enter__(self): return self
      def __exit__(self, *args): return False
      def extract_info(self, url, download=False):
        return {"id": VIDEO_ID, "automatic_captions": {"en": [{"url": "fake"}]}}
      def download(self, urls):
        raise stt.yt_dlp.utils.DownloadError("HTTP Error 429: Too Many Requests")
    with tempfile.TemporaryDirectory() as temp_dir, patch.object(stt.yt_dlp, "YoutubeDL", FakeYoutubeDL), patch.object(stt.time, "sleep", return_value=None):
      result = stt.yt_downloader_ytdlp(
        f"https://www.youtube.com/watch?v={VIDEO_ID}", temp_dir, VIDEO_ID, max_retries=1,
        config={"BASE_PATH": temp_dir, "WORK_PATH": temp_dir, "JOB_SUBS_DIR": temp_dir},
      )
    self.assertEqual(result[0], "__SOURCE_UNAVAILABLE__")
    self.assertTrue(captured["skip_download"])

  def test_generation_cache_binds_source_and_refuses_symlink_malformed_and_oversized(self):
    from job_workspace import VideoJobWorkspace
    with tempfile.TemporaryDirectory() as temp_dir:
      workspace = VideoJobWorkspace(temp_dir, VIDEO_ID)
      args = dict(input_revision="rev", model="model", prompt_sha256="prompt", summary="cached")
      workspace.save_generation_result(**args)
      self.assertEqual(workspace.load_generation_result(input_revision="rev", model="model", prompt_sha256="prompt"), "cached")
      self.assertIsNone(workspace.load_generation_result(input_revision="other", model="model", prompt_sha256="prompt"))
      self.assertIsNone(workspace.load_generation_result(input_revision="rev", model="other", prompt_sha256="prompt"))
      self.assertIsNone(workspace.load_generation_result(input_revision="rev", model="model", prompt_sha256="other"))
      path = Path(workspace.generation_cache_path())
      import json
      record = json.loads(path.read_text(encoding="utf-8"))
      record["summary"] = "substituted"
      path.write_text(json.dumps(record), encoding="utf-8")
      self.assertIsNone(workspace.load_generation_result(input_revision="rev", model="model", prompt_sha256="prompt"))
      path.unlink()
      target = Path(temp_dir) / "outside.json"
      target.write_text("{}", encoding="utf-8")
      path.symlink_to(target)
      self.assertIsNone(workspace.load_generation_result(input_revision="rev", model="model", prompt_sha256="prompt"))
      path.unlink()
      path.write_text("{", encoding="utf-8")
      self.assertIsNone(workspace.load_generation_result(input_revision="rev", model="model", prompt_sha256="prompt"))
      path.write_bytes(b" " * (64 * 1024 * 1024 + 1))
      self.assertIsNone(workspace.load_generation_result(input_revision="rev", model="model", prompt_sha256="prompt"))

  def test_generation_cache_reads_open_file_when_path_is_replaced(self):
    import hashlib
    import json
    import os
    import job_workspace
    from job_workspace import VideoJobWorkspace

    with tempfile.TemporaryDirectory() as temp_dir:
      workspace = VideoJobWorkspace(temp_dir, VIDEO_ID)
      args = dict(input_revision="rev", model="model", prompt_sha256="prompt")
      workspace.save_generation_result(**args, summary="canonical cached summary")
      cache_path = Path(workspace.generation_cache_path())
      replacement_path = cache_path.with_suffix(".replacement")
      replacement = json.loads(cache_path.read_text(encoding="utf-8"))
      replacement["summary"] = "substituted after open"
      replacement["summary_sha256"] = hashlib.sha256(replacement["summary"].encode("utf-8")).hexdigest()
      replacement_path.write_text(json.dumps(replacement), encoding="utf-8")
      real_open = os.open
      replaced = False

      def open_then_replace(path, flags, *open_args, **open_kwargs):
        nonlocal replaced
        fd = real_open(path, flags, *open_args, **open_kwargs)
        if os.fspath(path) == str(cache_path) and not replaced:
          replaced = True
          os.replace(replacement_path, cache_path)
        return fd

      with patch.object(job_workspace.os, "open", side_effect=open_then_replace):
        result = workspace.load_generation_result(**args)

      self.assertTrue(replaced)
      self.assertEqual(result, "canonical cached summary")

  def test_failed_auto_caption_status_remains_selectable_for_retry(self):
    import pandas as pd
    import channel_crawl
    queue = pd.DataFrame([{"video_id": VIDEO_ID, "status": "queued", "retry_count": 0, "last_attempted_at": "", "done_at": "", "last_error": "", "published_at": "", "discovered_at": ""}])
    failed = channel_crawl.apply_result_to_queue(queue, VIDEO_ID, "SOURCE_UNAVAILABLE", "HTTP 429")
    self.assertEqual(failed.iloc[0]["status"], "failed")
    self.assertEqual(len(channel_crawl.select_process_candidates(failed, max_retries=3)), 1)

  def test_retry_checks_persisted_summary_before_any_llm_stage(self):
    import pandas as pd
    import main

    class UploadOnceThenSucceed:
      def __init__(self): self.uploads = []
      def already_uploaded(self, **kwargs): return False
      def upload_video(self, **kwargs):
        self.uploads.append(kwargs)
        if len(self.uploads) == 1:
            raise DriveUploadError("synthetic canonical manifest SHA-256 readback failure")
        return {"verified": True}

    class Preprocessor:
      def minimize(self, role, query, text, **kwargs): return "concise transcript"

    class Usage:
      def to_dict(self): return {"total_tokens": 1}

    for mode in ("legacy_two_stage", "direct_luna"):
      with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        for name in ("audio", "full", "smm", "md", "logs", "hf"):
          (root / name).mkdir()
        sink = UploadOnceThenSucceed()
        calls = []

        def download(**kwargs):
          subs_dir = Path(kwargs["config"]["JOB_SUBS_DIR"])
          subs_dir.mkdir(parents=True, exist_ok=True)
          subs = subs_dir / f"{VIDEO_ID}.en.vtt"
          subs.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\ncaption text\n", encoding="utf-8")
          return (None, "title", VIDEO_ID, 0, "", "", str(subs), "auto", "en", "channel", "20240101")

        config = {
          "WORK_PATH": temp_dir, "DATA_ROOT": temp_dir, "USE_JOB_WORKSPACE": True,
          "TRANSCRIPT_CACHE_ENABLED": False, "SAVE_FULL_WHEN_AUTO_SUBS": False,
          "LLM_PIPELINE_MODE": mode, "DRIVE_API_SINK": sink,
          "DIRECT_LLM_MODEL": "fake-direct", "MAIN_LLM_OUTPUT_SUFFIX": "5-mini",
        }
        args = (
          f"https://www.youtube.com/watch?v={VIDEO_ID}", config, None,
          types.SimpleNamespace(primary_model="fake-legacy", primary_provider="fake", has_fallback=False),
          pd.DataFrame(), temp_dir, str(root / "audio"), str(root / "full"),
          str(root / "smm"), str(root / "md"), str(root / "logs"), str(root / "hf"),
        )
        with ExitStack() as stack:
          stack.enter_context(patch.dict("os.environ", {"P03_DRIVE_BACKEND": "api", "P03_REPO_COMMIT": "synthetic-test-revision"}))
          stack.enter_context(patch.object(main.stt, "extract_youtube_id", return_value=VIDEO_ID))
          stack.enter_context(patch.object(main.stt, "clear_last_ytdlp_failure"))
          stack.enter_context(patch.object(main.stt, "yt_downloader", side_effect=download))
          stack.enter_context(patch.object(main.stt, "subtitle_file_to_plain_text", return_value="caption text"))
          stack.enter_context(patch.object(main.stt, "sanitize_filename", side_effect=lambda value, **kwargs: value))
          stack.enter_context(patch.object(main.stt, "count_tokens", return_value=1))
          stack.enter_context(patch.object(main, "_build_transcript_cache", return_value=types.SimpleNamespace(put=lambda *a, **k: None)))
          stack.enter_context(patch.object(main, "summarize_with_pipeline", side_effect=lambda *a, **k: (calls.append(mode) or "# Title\nsummary", Usage())))
          stack.enter_context(patch.object(main, "_append_llm_usage"))
          stack.enter_context(patch.object(main, "_find_exact_durable_full_transcript", return_value=None))
          stack.enter_context(patch.object(main, "_read_durable_full_transcript", return_value=(None, None)))
          stack.enter_context(patch.object(main, "atomic_write_text_with_retry"))
          first = main.process_single_video(*args, preprocessor=Preprocessor())
          self.assertEqual(first.status, "file_error", first.error_message)
          self.assertTrue((root / "tmp/jobs" / VIDEO_ID / f"{VIDEO_ID}.en.vtt").is_file())
          second = main.process_single_video(*args, preprocessor=Preprocessor())

        self.assertEqual(second.status, "success")
        self.assertEqual(len(calls), 1, "cached retry must not make another summary LLM call")
        self.assertEqual(len(sink.uploads), 2, "upload should be attempted again from cached summary")
        self.assertFalse((root / "tmp/jobs" / VIDEO_ID / f"{VIDEO_ID}.en.vtt").exists(), "verified upload deletes the subtitle VTT")
        self.assertFalse((root / "tmp/jobs" / VIDEO_ID).exists(), "verified success cleans the job workspace")

  def test_verified_dedupe_marks_success_and_removes_vtt_before_return(self):
    import pandas as pd
    import main

    class VerifiedDedupeSink:
      def already_uploaded(self, **kwargs): return True
      def upload_video(self, **kwargs): raise AssertionError("dedupe must not upload")

    with tempfile.TemporaryDirectory() as temp_dir:
      root = Path(temp_dir)
      for name in ("audio", "full", "smm", "md", "logs", "hf"):
        (root / name).mkdir()
      sink = VerifiedDedupeSink()

      def download(**kwargs):
        subs_dir = Path(kwargs["config"]["JOB_SUBS_DIR"])
        subs_dir.mkdir(parents=True, exist_ok=True)
        subs = subs_dir / f"{VIDEO_ID}.en.vtt"
        subs.write_text("WEBVTT\ncaption text\n", encoding="utf-8")
        return (None, "title", VIDEO_ID, 0, "", "", str(subs), "auto", "en", "channel", "20240101")

      config = {"WORK_PATH": temp_dir, "DATA_ROOT": temp_dir, "USE_JOB_WORKSPACE": True,
                "TRANSCRIPT_CACHE_ENABLED": False, "SAVE_FULL_WHEN_AUTO_SUBS": False,
                "DRIVE_API_SINK": sink}
      args = (f"https://www.youtube.com/watch?v={VIDEO_ID}", config, None, None,
              pd.DataFrame(), temp_dir, str(root / "audio"), str(root / "full"),
              str(root / "smm"), str(root / "md"), str(root / "logs"), str(root / "hf"))
      with ExitStack() as stack:
        stack.enter_context(patch.dict("os.environ", {"P03_DRIVE_BACKEND": "api", "P03_REPO_COMMIT": "synthetic-test-revision"}))
        stack.enter_context(patch.object(main.stt, "extract_youtube_id", return_value=VIDEO_ID))
        stack.enter_context(patch.object(main.stt, "clear_last_ytdlp_failure"))
        stack.enter_context(patch.object(main.stt, "yt_downloader", side_effect=download))
        stack.enter_context(patch.object(main.stt, "subtitle_file_to_plain_text", return_value="caption text"))
        stack.enter_context(patch.object(main.stt, "sanitize_filename", side_effect=lambda value, **kwargs: value))
        stack.enter_context(patch.object(main, "_build_transcript_cache", return_value=types.SimpleNamespace(put=lambda *a, **k: None)))
        stack.enter_context(patch.object(main, "_find_exact_durable_full_transcript", return_value=None))
        stack.enter_context(patch.object(main, "_read_durable_full_transcript", return_value=(None, None)))
        result = main.process_single_video(*args)

      self.assertEqual(result.status, "already_existed")
      self.assertEqual(result.stage, "drive_dedupe")
      self.assertFalse((root / "tmp/jobs" / VIDEO_ID / f"{VIDEO_ID}.en.vtt").exists(), "verified dedupe deletes the subtitle VTT")
      self.assertFalse((root / "tmp/jobs" / VIDEO_ID).exists(), "verified dedupe cleans the job workspace")


if __name__ == "__main__":
    unittest.main()
