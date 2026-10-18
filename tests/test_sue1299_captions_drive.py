"""Offline regression checks for the SUE-1299 Linux/API boundary."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from drive_api_sink import DriveApiSink, DriveUploadError, SOURCE_FOLDER_ID

VIDEO_ID = "abcdefghijk"


class _Request:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class _FakeDrive:
    def __init__(self, mismatch=False, mismatch_name="", bad_readback=""): self.items = {}; self.n = 0; self.mismatch = mismatch; self.mismatch_name = mismatch_name; self.bad_readback = bad_readback
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
            item = self.items[fileId]; item.update(body or {})
            if media_body is not None:
                data = getattr(media_body, "data", b""); item["data"] = data; item["size"] = str(len(data))
                bad = self.mismatch or item.get("name") == self.mismatch_name
                item["md5Checksum"] = "bad" if bad else __import__("hashlib").md5(data).hexdigest()
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

  def test_failed_auto_caption_status_remains_selectable_for_retry(self):
    import pandas as pd
    import channel_crawl
    queue = pd.DataFrame([{"video_id": VIDEO_ID, "status": "queued", "retry_count": 0, "last_attempted_at": "", "done_at": "", "last_error": "", "published_at": "", "discovered_at": ""}])
    failed = channel_crawl.apply_result_to_queue(queue, VIDEO_ID, "SOURCE_UNAVAILABLE", "HTTP 429")
    self.assertEqual(failed.iloc[0]["status"], "failed")
    self.assertEqual(len(channel_crawl.select_process_candidates(failed, max_retries=3)), 1)

  def test_retry_checks_persisted_summary_before_any_llm_stage(self):
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    process = source[source.index("def process_single_video("):source.index("def process_videos(")]
    cache_check = process.index("cached_response = (job_workspace.load_generation_result")
    preprocess = process.index("# Step 4: Token minimization")
    summarize = process.index("response, usage = summarize_with_pipeline(")
    self.assertLess(cache_check, preprocess)
    self.assertIn("if cached_response is not None:", process[cache_check:summarize])
    self.assertIn("if cached_response is None and job_workspace is not None:", process)

  def test_verified_dedupe_marks_success_and_removes_vtt_before_return(self):
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    process = source[source.index("def process_single_video("):source.index("def process_videos(")]
    dedupe = process[process.index("if api_sink.already_uploaded"):process.index("direct_mode =", process.index("if api_sink.already_uploaded"))]
    self.assertIn("delete_subtitle_file(subs_path)", dedupe)
    self.assertIn("job_workspace.remove_subtitle_files()", dedupe)
    self.assertIn("pipeline_success = True", dedupe)


if __name__ == "__main__":
    unittest.main()
