"""Synthetic coverage for the API-backed canonical P03 manifest writer."""
from __future__ import annotations

import hashlib
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from drive_api_sink import DriveApiSink, DriveUploadError, MANIFEST_FILE_ID, SUMMARY_FOLDER_ID
from scripts.drive_yt_summary.manifest import (
    build_inventory_record,
    merge_manifest_source,
    parse_manifest_yaml,
    serialize_manifest_yaml,
)


class _Request:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class _Media:
    def __init__(self, stream, **_kwargs):
        self.data = stream.read()


def _install_fake_media(monkeypatch):
    google = types.ModuleType("googleapiclient")
    http = types.ModuleType("googleapiclient.http")
    http.MediaIoBaseUpload = _Media
    monkeypatch.setitem(sys.modules, "googleapiclient", google)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http)


class _FakeDrive:
    def __init__(self, *, manifest=True, corrupt_manifest_readback=False):
        self.items = {}
        self.next_id = 10
        self.manifest_update_count = 0
        self.created_manifest_count = 0
        self.corrupt_manifest_readback = corrupt_manifest_readback
        self.manifest_get_count = 0
        self.on_second_manifest_get = None
        if manifest:
            self._set_manifest({"version": 1, "generated_at": "legacy", "items": []}, revision="1")

    def _set_manifest(self, doc, revision=None):
        payload = serialize_manifest_yaml(doc)
        self.items[MANIFEST_FILE_ID] = {
            "id": MANIFEST_FILE_ID,
            "name": "manifest.yaml",
            "mimeType": "text/yaml",
            "parents": [SUMMARY_FOLDER_ID],
            "data": payload,
            "size": str(len(payload)),
            "md5Checksum": hashlib.md5(payload).hexdigest(),
            "headRevisionId": str(revision or "1"),
        }

    def files(self):
        return self

    def list(self, q="", **_kwargs):
        def run():
            parent = q.split("'", 2)[1] if "'" in q else ""
            name = q.split("name='", 1)[1].split("'", 1)[0] if "name='" in q else None
            folder = "mimeType='application/vnd.google-apps.folder'" in q
            return {"files": [
                {key: value for key, value in item.items() if key in {"id", "name", "mimeType", "parents", "appProperties"}}
                for item in self.items.values()
                if parent in item.get("parents", []) and not item.get("trashed") and
                (name is None or item.get("name") == name) and
                (not folder or item.get("mimeType") == "application/vnd.google-apps.folder")
            ]}
        return _Request(run)

    def create(self, body, media_body=None, **_kwargs):
        def run():
            self.next_id += 1
            file_id = str(self.next_id)
            data = getattr(media_body, "data", b"") if media_body is not None else b""
            item = {**body, "id": file_id, "data": data, "size": str(len(data)),
                    "md5Checksum": hashlib.md5(data).hexdigest(), "headRevisionId": "1"}
            self.items[file_id] = item
            if body.get("name") == "manifest.yaml":
                self.created_manifest_count += 1
            return {"id": file_id}
        return _Request(run)

    def update(self, fileId, body=None, media_body=None, **_kwargs):
        def run():
            item = self.items[fileId]
            item.update(body or {})
            if media_body is not None:
                data = getattr(media_body, "data", b"")
                item["data"] = data
                item["size"] = str(len(data))
                item["md5Checksum"] = hashlib.md5(data).hexdigest()
                item["headRevisionId"] = str(int(item.get("headRevisionId", "0")) + 1)
                if fileId == MANIFEST_FILE_ID:
                    self.manifest_update_count += 1
            return {"id": fileId}
        return _Request(run)

    def get(self, fileId, **_kwargs):
        def run():
            if fileId == MANIFEST_FILE_ID:
                self.manifest_get_count += 1
                if self.manifest_get_count == 2 and self.on_second_manifest_get:
                    hook, self.on_second_manifest_get = self.on_second_manifest_get, None
                    hook()
            item = dict(self.items[fileId])
            item["parents"] = list(item.get("parents", []))
            item["appProperties"] = dict(item.get("appProperties", {}))
            return item
        return _Request(run)

    def get_media(self, fileId):
        def run():
            payload = self.items[fileId].get("data", b"")
            if fileId == MANIFEST_FILE_ID and self.corrupt_manifest_readback and self.manifest_update_count:
                return payload + b"\n# corrupt readback"
            return payload
        return _Request(run)


@pytest.fixture(autouse=True)
def fake_media_and_lock(tmp_path, monkeypatch):
    _install_fake_media(monkeypatch)
    monkeypatch.setenv("P03_MANIFEST_FILE_ID", MANIFEST_FILE_ID)
    monkeypatch.setenv("P03_MANIFEST_LOCK_PATH", str(tmp_path / "manifest.lock"))


def _merge(sink, *, file, video_id, summary, drive_file_id="drive-source"):
    return sink._merge_manifest_summary(
        summary_name=file,
        summary=summary,
        video_id=video_id,
        drive_file_id=drive_file_id,
    )


def test_new_summary_content_revision_and_identical_merge_are_revision_safe():
    drive = _FakeDrive()
    sink = DriveApiSink(drive)
    first_bytes = b"# first summary\n"
    first = _merge(sink, file="title_abcdefghijk.md", video_id="abcdefghijk", summary=first_bytes)
    first_item = sink.read_manifest()["items"][0]
    assert first["action"] == "added"
    assert first_item["revision"] == 1
    assert first_item["finalized_at"].endswith("Z")
    assert "content_revision_at" not in first_item

    updates = drive.manifest_update_count
    same = _merge(sink, file="title_abcdefghijk.md", video_id="abcdefghijk", summary=first_bytes)
    assert same["action"] == "unchanged"
    assert drive.manifest_update_count == updates

    changed = _merge(sink, file="title_abcdefghijk.md", video_id="abcdefghijk", summary=b"# revised summary\n")
    revised = sink.read_manifest()["items"][0]
    assert changed["action"] == "revised"
    assert revised["revision"] == 2
    assert revised["content_revision_at"].endswith("Z")
    assert revised["finalized_at"] == first_item["finalized_at"]


def test_legacy_item_is_not_timestamp_backfilled_or_rewritten_for_same_hash():
    legacy = {"file": "title_abcdefghijk.md", "title": "legacy", "hash": hashlib.sha256(b"legacy bytes").hexdigest(),
              "revision": 4, "video_id": "abcdefghijk", "legacy_extra": {"kept": True}}
    drive = _FakeDrive()
    drive._set_manifest({"version": 1, "items": [legacy]}, revision="4")
    sink = DriveApiSink(drive)
    updates = drive.manifest_update_count
    _merge(sink, file=legacy["file"], video_id="abcdefghijk", summary=b"legacy bytes")
    manifest_item = sink.read_manifest()["items"][0]
    assert manifest_item == legacy
    assert drive.manifest_update_count == updates


def test_dedupe_repairs_missing_manifest_entry_as_recovered_unknown_finalization():
    drive = _FakeDrive()
    sink = DriveApiSink(drive)
    summary = b"# exact stored summary\n"
    sink.upload_video(summary_name="title_abcdefghijk.md", summary=summary, transcript=b"caption",
                      metadata={}, video_id="abcdefghijk")
    drive._set_manifest({"version": 1, "items": []}, revision="9")

    assert sink.already_uploaded(video_id="abcdefghijk", transcript=b"caption") is True
    item = sink.read_manifest()["items"][0]
    assert item["hash"] == hashlib.sha256(summary).hexdigest()
    assert item["revision"] == 1
    assert item["finalized_at"] is None
    assert item["recovered_at"].endswith("Z")


def test_manifest_readback_failure_raises_and_never_returns_upload_success():
    drive = _FakeDrive(corrupt_manifest_readback=True)
    sink = DriveApiSink(drive)
    with pytest.raises(DriveUploadError, match="SHA-256 readback"):
        sink.upload_video(summary_name="title_abcdefghijk.md", summary=b"summary", transcript=b"caption",
                          metadata={}, video_id="abcdefghijk")


def test_missing_fixed_manifest_is_never_created():
    drive = _FakeDrive(manifest=False)
    sink = DriveApiSink(drive)
    with pytest.raises(DriveUploadError, match="canonical manifest lookup failed"):
        _merge(sink, file="title_abcdefghijk.md", video_id="abcdefghijk", summary=b"summary")
    assert drive.created_manifest_count == 0


def test_concurrent_manifest_writers_keep_both_sources():
    drive = _FakeDrive()
    sinks = [DriveApiSink(drive), DriveApiSink(drive)]
    barrier = threading.Barrier(2)

    def write(index):
        barrier.wait()
        video_id = f"video{index:06d}"
        _merge(sinks[index], file=f"title_{video_id}.md", video_id=video_id,
               summary=f"summary {index}".encode(), drive_file_id=f"drive-{index}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, range(2)))
    items = drive and sinks[0].read_manifest()["items"]
    assert {item["video_id"] for item in items} == {"video000000", "video000001"}
    assert len(items) == 2


def test_head_revision_or_md5_change_before_update_remerges_latest_document():
    drive = _FakeDrive()
    sink = DriveApiSink(drive)

    def external_update():
        doc = parse_manifest_yaml(drive.items[MANIFEST_FILE_ID]["data"])
        doc["items"].append({"file": "external_abcdefghijk.md", "title": "external",
                             "hash": hashlib.sha256(b"external").hexdigest(), "revision": 1,
                             "video_id": "abcdefghijk"})
        drive._set_manifest(doc, revision=str(int(drive.items[MANIFEST_FILE_ID]["headRevisionId"]) + 1))

    drive.on_second_manifest_get = external_update
    _merge(sink, file="local_lmnopqrst.md", video_id="lmnopqrst", summary=b"local", drive_file_id="local-id")
    items = sink.read_manifest()["items"]
    assert {item["video_id"] for item in items} == {"abcdefghijk", "lmnopqrst"}


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"succeeded": 1, "failed": 0, "unresolved": 0, "discovery_complete": True,
          "all_successes_present": True, "manifest_readback_verified": True, "completed": True}, True),
        ({"succeeded": 1, "failed": 1, "unresolved": 0, "discovery_complete": True,
          "all_successes_present": True, "manifest_readback_verified": True, "completed": True}, False),
        ({"succeeded": 1, "failed": 0, "unresolved": 0, "discovery_complete": False,
          "all_successes_present": True, "manifest_readback_verified": True, "completed": True}, False),
        ({"succeeded": 1, "failed": 0, "unresolved": 0, "discovery_complete": True,
          "all_successes_present": True, "manifest_readback_verified": True, "completed": False}, False),
    ],
)
def test_inventory_completeness_requires_full_verified_scope(kwargs, expected):
    record = build_inventory_record(
        producer_run_id="run-1", coverage_started_at="2026-10-23T00:00:00Z", cutoff={"after": "cursor"},
        now="2026-10-23T00:01:00Z", **kwargs,
    )
    assert record["inventory_complete"] is expected
    assert record["coverage_completed_at"] == ("2026-10-23T00:01:00Z" if kwargs["completed"] else None)


def test_inventory_write_is_readback_verified_and_revision_monotonic():
    drive = _FakeDrive()
    sink = DriveApiSink(drive)
    _merge(sink, file="title_abcdefghijk.md", video_id="abcdefghijk", summary=b"success")
    started = build_inventory_record(producer_run_id="run-1", coverage_started_at="2026-10-23T00:00:00Z",
                                     cutoff={"mode": "channel_cursor"}, completed=False)
    first = sink.write_inventory(started, start=True)
    assert first["inventory_revision"] == 1
    completed = build_inventory_record(
        producer_run_id="run-1", coverage_started_at=started["coverage_started_at"], cutoff=started["cutoff"],
        succeeded=1, discovery_complete=True, all_successes_present=True,
        manifest_readback_verified=True, completed=True, now="2026-10-23T00:01:00Z",
    )
    final = sink.write_inventory(completed, success_video_ids=["abcdefghijk"])
    assert final["inventory_revision"] == 1
    assert final["inventory_complete"] is True
    next_run = {**started, "producer_run_id": "run-2"}
    second = sink.write_inventory(next_run, start=True)
    assert second["inventory_revision"] == 2


@pytest.mark.parametrize(("failed_channel", "expected_complete"), [(None, True), ("UC" + "b" * 22, False)])
def test_channel_discovery_report_requires_every_channel_call(failed_channel, expected_complete, monkeypatch):
    import pandas as pd
    import channel_crawl

    channels = [
        {"channel_id": "UC" + "a" * 22, "channel_url": "https://youtube.test/a",
         "uploads_playlist_id": "UU" + "a" * 22, "last_discovered_published_at": "2026-10-22T00:00:00Z"},
        {"channel_id": "UC" + "b" * 22, "channel_url": "https://youtube.test/b",
         "uploads_playlist_id": "UU" + "b" * 22, "last_discovered_published_at": "2026-10-22T00:00:00Z"},
    ]
    monkeypatch.setattr(channel_crawl, "load_channel_df", lambda _root: channels)
    monkeypatch.setattr(channel_crawl, "load_crawl_queue_df", lambda _root: pd.DataFrame())
    monkeypatch.setattr(channel_crawl, "reconcile_queue_with_output_df", lambda queue, *_args: queue)
    monkeypatch.setattr(channel_crawl, "expire_live_replay_rows", lambda queue, *_args: queue)
    monkeypatch.setattr(channel_crawl, "save_channel_df", lambda *_args: None)
    monkeypatch.setattr(channel_crawl, "save_crawl_queue_df", lambda *_args: None)
    monkeypatch.setattr(channel_crawl, "select_process_candidates", lambda *_args: pd.DataFrame())

    def fetch(_key, channel_id, uploads_id=None, cursor_dt=None, discovery_status=None):
        if channel_id == failed_channel:
            discovery_status["complete"] = False
        return [], "", uploads_id or ""

    monkeypatch.setattr(channel_crawl, "fetch_channel_via_api", fetch)
    report = {}
    channel_crawl.build_queue_and_get_candidates(
        "/synthetic", {"YOUTUBE_API_KEY": "fake", "FILTERING_SHORTS_MINUTES": 0},
        pd.DataFrame(columns=["v_id"]), discovery_report=report,
    )
    assert report["channels_succeeded"] == (2 if failed_channel is None else 1)
    assert report["complete"] is expected_complete
