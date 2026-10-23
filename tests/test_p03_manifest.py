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

from drive_api_sink import DriveApiSink, DriveUploadError, INVENTORY_FILE_NAME, MANIFEST_FILE_ID, SUMMARY_FOLDER_ID
from scripts.drive_yt_summary.manifest import (
    build_inventory_record,
    classify_inventory_status,
    inventory_completion_decision,
    merge_manifest_source,
    parse_manifest_yaml,
    reconcile_inventory_scope,
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
    def __init__(self, *, manifest=True, corrupt_manifest_readback=False, corrupt_inventory_readback=False):
        self.items = {}
        self.next_id = 10
        self.manifest_update_count = 0
        self.created_manifest_count = 0
        self.corrupt_manifest_readback = corrupt_manifest_readback
        self.corrupt_manifest_after_update_count = None
        self.corrupt_inventory_readback = corrupt_inventory_readback
        self.created_inventory_count = 0
        self.manifest_get_count = 0
        self.on_second_manifest_get = None
        self.on_after_manifest_update = None
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
            if body.get("name") == INVENTORY_FILE_NAME:
                self.created_inventory_count += 1
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
                    if self.on_after_manifest_update:
                        hook, self.on_after_manifest_update = self.on_after_manifest_update, None
                        hook()
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
            if (fileId == MANIFEST_FILE_ID and self.corrupt_manifest_readback and self.manifest_update_count and
                    (self.corrupt_manifest_after_update_count is None or
                     self.manifest_update_count > self.corrupt_manifest_after_update_count)):
                return payload + b"\n# corrupt readback"
            if (self.items[fileId].get("name") == INVENTORY_FILE_NAME and
                    self.corrupt_inventory_readback):
                return payload + b" "
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


def test_recovered_existing_content_revision_keeps_actual_revision_time_unknown():
    original = {"file": "title_abcdefghijk.md", "title": "old", "hash": hashlib.sha256(b"old").hexdigest(),
                "revision": 4, "video_id": "abcdefghijk", "content_revision_at": "2026-10-01T00:00:00Z"}
    recovered, change, changed = merge_manifest_source(
        {"version": 1, "items": [original]}, file=original["file"], title="new",
        content_hash=hashlib.sha256(b"new recovered bytes").hexdigest(), video_id="abcdefghijk",
        drive_file_id="drive-source", recovered=True, now="2026-10-23T12:00:00Z",
    )
    item = recovered["items"][0]
    assert changed is True
    assert change["action"] == "revised"
    assert item["revision"] == 5
    assert "content_revision_at" not in item
    assert item["recovered_at"] == "2026-10-23T12:00:00Z"


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


def test_post_update_clobber_is_detected_and_missing_pre_read_items_are_restored():
    drive = _FakeDrive()
    old_item = {"file": "old_abcdefghijk.md", "title": "Old", "hash": hashlib.sha256(b"old").hexdigest(),
                "revision": 1, "video_id": "abcdefghijk", "drive_file_id": "old-drive-id"}
    drive._set_manifest({"version": 1, "items": [old_item]}, revision="1")
    sink = DriveApiSink(drive)

    def racing_writer_drops_pre_read_item():
        current = parse_manifest_yaml(drive.items[MANIFEST_FILE_ID]["data"])
        current["items"] = [item for item in current["items"] if item.get("drive_file_id") != "old-drive-id"]
        drive._set_manifest(current, revision=str(int(drive.items[MANIFEST_FILE_ID]["headRevisionId"]) + 1))

    drive.on_after_manifest_update = racing_writer_drops_pre_read_item
    _merge(sink, file="local_lmnopqrst.md", video_id="lmnopqrst", summary=b"local", drive_file_id="local-id")
    items = sink.read_manifest()["items"]
    assert {item.get("drive_file_id") for item in items} == {"old-drive-id", "local-id"}


def test_post_update_clobber_restores_missing_fields_on_matching_item():
    drive = _FakeDrive()
    old_item = {
        "file": "old_abcdefghijk.md", "title": "Old", "hash": hashlib.sha256(b"old").hexdigest(),
        "revision": 1, "video_id": "abcdefghijk", "drive_file_id": "old-drive-id",
        "custom_field": {"retain": True, "nested": {"also_retain": "yes"}},
    }
    drive._set_manifest({"version": 1, "items": [old_item]}, revision="1")
    sink = DriveApiSink(drive)

    def racing_writer_drops_pre_read_field():
        current = parse_manifest_yaml(drive.items[MANIFEST_FILE_ID]["data"])
        current["items"][0]["custom_field"]["nested"].pop("also_retain")
        drive._set_manifest(current, revision=str(int(drive.items[MANIFEST_FILE_ID]["headRevisionId"]) + 1))

    drive.on_after_manifest_update = racing_writer_drops_pre_read_field
    result = _merge(sink, file="local_lmnopqrst.md", video_id="lmnopqrst", summary=b"local",
                    drive_file_id="local-id")

    items = sink.read_manifest()["items"]
    restored = next(item for item in items if item.get("drive_file_id") == "old-drive-id")
    assert result["action"] == "unchanged"
    assert restored["custom_field"] == {"retain": True, "nested": {"also_retain": "yes"}}
    assert drive.manifest_update_count >= 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"succeeded": 1, "failed": 0, "unresolved": 0, "discovery_complete": True,
         "all_successes_present": True, "manifest_readback_verified": True, "completed": True},
        {"succeeded": 1, "failed": 1, "unresolved": 0, "discovery_complete": True,
         "all_successes_present": True, "manifest_readback_verified": True, "completed": True},
        {"succeeded": 1, "failed": 0, "unresolved": 0, "discovery_complete": False,
         "all_successes_present": True, "manifest_readback_verified": True, "completed": True},
        {"succeeded": 1, "failed": 0, "unresolved": 0, "discovery_complete": True,
         "all_successes_present": True, "manifest_readback_verified": True, "completed": False},
    ],
)
def test_inventory_manifest_record_never_self_asserts_completion(kwargs):
    record = build_inventory_record(
        producer_run_id="run-1", coverage_started_at="2026-10-23T00:00:00Z", cutoff={"after": "cursor"},
        now="2026-10-23T00:01:00Z", **kwargs,
    )
    assert record["inventory_complete"] is False
    assert record["manifest_readback_verified"] is False
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
        coverage_state_valid=True, manifest_readback_verified=True, completed=True, now="2026-10-23T00:01:00Z",
    )
    final = sink.write_inventory(completed, success_video_ids=["abcdefghijk"])
    assert final["inventory_revision"] == 1
    assert final["inventory_complete"] is True
    assert final["inventory_file_readback_verified"] is True
    manifest_inventory = sink.read_manifest()["inventory"]
    assert manifest_inventory["inventory_complete"] is False
    assert manifest_inventory["manifest_readback_verified"] is False
    evidence = sink.read_inventory_evidence()
    assert evidence["inventory"]["inventory_complete"] is True
    assert evidence["manifest_sha256"] == hashlib.sha256(drive.items[MANIFEST_FILE_ID]["data"]).hexdigest()
    assert drive.created_inventory_count == 1
    sink.write_inventory(completed, success_video_ids=["abcdefghijk"])
    assert drive.created_inventory_count == 1
    next_run = {**started, "producer_run_id": "run-2"}
    second = sink.write_inventory(next_run, start=True)
    assert second["inventory_revision"] == 2


def test_manifest_readback_failure_never_publishes_completion_evidence():
    drive = _FakeDrive()
    sink = DriveApiSink(drive)
    started = build_inventory_record(producer_run_id="run-1", coverage_started_at="2026-10-23T00:00:00Z",
                                     cutoff={"mode": "channel_cursor"}, completed=False)
    sink.write_inventory(started, start=True)
    drive.corrupt_manifest_readback = True
    drive.corrupt_manifest_after_update_count = drive.manifest_update_count
    final = build_inventory_record(
        producer_run_id="run-1", coverage_started_at=started["coverage_started_at"], cutoff=started["cutoff"],
        succeeded=0, discovery_complete=True, coverage_state_valid=True,
        completed=True, now="2026-10-23T00:01:00Z",
    )
    with pytest.raises(DriveUploadError, match="readback"):
        sink.write_inventory(final)
    assert drive.created_inventory_count == 0
    assert parse_manifest_yaml(drive.items[MANIFEST_FILE_ID]["data"])["inventory"]["inventory_complete"] is False


def test_inventory_file_readback_failure_cannot_be_trusted():
    drive = _FakeDrive(corrupt_inventory_readback=True)
    sink = DriveApiSink(drive)
    _merge(sink, file="title_abcdefghijk.md", video_id="abcdefghijk", summary=b"success")
    started = build_inventory_record(producer_run_id="run-1", coverage_started_at="2026-10-23T00:00:00Z",
                                     cutoff={"mode": "channel_cursor"}, completed=False)
    sink.write_inventory(started, start=True)
    complete = build_inventory_record(
        producer_run_id="run-1", coverage_started_at=started["coverage_started_at"], cutoff=started["cutoff"],
        succeeded=1, discovery_complete=True, all_successes_present=True, completed=True,
        coverage_state_valid=True,
        now="2026-10-23T00:01:00Z",
    )
    with pytest.raises(DriveUploadError, match="inventory evidence"):
        sink.write_inventory(complete, success_video_ids=["abcdefghijk"])
    assert sink.read_manifest()["inventory"]["inventory_complete"] is False
    with pytest.raises(DriveUploadError, match="inventory evidence verification failed"):
        sink.read_inventory_evidence()


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        ("manual_input", "manual_input_read_failed"),
        ("processed_state", "manual_processed_state_read_failed"),
        ("manual_ledger", "manual_ledger_read_failed"),
    ],
)
def test_process_videos_keeps_unreadable_manual_scope_out_of_complete_inventory(
        tmp_path, monkeypatch, failure, expected_reason):
    import pandas as pd
    import main
    import processed_video_registry as pvr

    data = tmp_path / "data"
    data.mkdir()
    pvr.write_registry(pd.DataFrame(columns=pvr.REGISTRY_COLUMNS), pvr.registry_path(data))
    (data / "input_df.csv").write_text(
        "date,url,category\n2026-10-22,https://youtu.be/manual00001,manual\n",
        encoding="cp949",
    )
    base, md = tmp_path / "base", tmp_path / "md"
    base.mkdir()
    md.mkdir()

    _install_fake_media(monkeypatch)
    sink = DriveApiSink(_FakeDrive())

    def empty_channel_queue(_root, _config, _output, *, discovery_report=None, **_kwargs):
        discovery_report.update({
            "enabled": True,
            "channel_count": 1,
            "channels_succeeded": 1,
            "channels_failed": [],
            "cursor_blocked_channels": [],
            "unresolved": 0,
            "complete": True,
            "cutoff": {"mode": "channel_cursor"},
            "queue_scope_video_ids": [],
            "retry_exhausted_video_ids": [],
            "queue_scope_unresolved": 0,
        })
        return pd.DataFrame(), pd.DataFrame(columns=["url"]), []

    monkeypatch.setattr(main, "initialize_clients", lambda _config: (None, None, None))
    monkeypatch.setattr(main.channel_crawl, "inventory_cutoff_snapshot",
                        lambda *_args: {"mode": "channel_cursor"})
    monkeypatch.setattr(main.channel_crawl, "build_queue_and_get_candidates", empty_channel_queue)
    monkeypatch.setattr(main.channel_crawl, "load_channel_df", lambda _root: [])
    monkeypatch.setattr(main, "load_output_df_only",
                        lambda _root: pd.DataFrame(columns=["v_id", "url", "status"]))
    monkeypatch.setattr(pvr, "read_sheet_marked_ids", lambda: set())
    monkeypatch.setattr(main, "_run_batch_cleanup", lambda *_args, **_kwargs: None)

    if failure == "manual_input":
        original_read_csv = main._read_csv_with_retry

        def fail_manual_csv(path, *args, **kwargs):
            if str(path).endswith("input_df.csv"):
                raise OSError("injected manual CSV read failure")
            return original_read_csv(path, *args, **kwargs)

        monkeypatch.setattr(main, "_read_csv_with_retry", fail_manual_csv)
    elif failure == "processed_state":
        monkeypatch.setattr(pvr, "processed_id_union",
                            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                                OSError("injected processed-state read failure")))
    else:
        monkeypatch.setattr(main.manual_video_input, "read_ledger",
                            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                                OSError("injected manual ledger read failure")))

    monkeypatch.setenv("P03_DRIVE_BACKEND", "api")
    monkeypatch.setenv("P03_DRIVE_SYNC_ENABLED", "0")
    monkeypatch.setenv("P03_MANUAL_SINCE", "2026-10-01")
    monkeypatch.delenv("P03_SHEET_IMPORT_ENABLED", raising=False)
    config = {
        "BASE_PATH": str(base),
        "DATA_ROOT": str(data),
        "WORK_PATH": str(base),
        "HF_HOME": str(tmp_path / "hf"),
        "OUTPUT_MD_PATH": str(md),
        "CHANNEL_CRAWL": True,
        "YOUTUBE_API_KEY": "fixture",
        "DRIVE_API_SINK": sink,
    }

    if failure == "manual_ledger":
        with pytest.raises(OSError, match="injected manual ledger read failure"):
            main.process_videos(config)
    else:
        main.process_videos(config)

    inventory = sink.read_manifest()["inventory"]
    assert inventory["inventory_complete"] is False
    assert inventory["source_counts"]["unresolved"] >= 1
    assert inventory["source_counts"]["scope_count"] >= 1
    assert expected_reason in inventory["incomplete_reason"]
    assert sink.read_inventory_evidence() is None


def test_inventory_scope_counts_claimed_deferred_and_retry_exhausted_sources():
    reconciled = reconcile_inventory_scope(
        {"success0001", "claim000001", "limit000001", "retry000001", "failed00001", "skip000001"},
        {
            "success0001": "success",
            "retry000001": "retry_exhausted",
            "failed00001": "file_error",
            "skip000001": "passed_shorts",
        },
    )
    assert reconciled["succeeded"] == 2
    assert reconciled["failed"] == 2
    assert reconciled["unresolved"] == 2  # active claim and max-video deferral
    assert reconciled["succeeded"] + reconciled["failed"] + reconciled["unresolved"] == reconciled["scope_count"]
    assert reconciled["manifest_succeeded"] == 1
    assert reconciled["terminal_skipped"] == 1
    assert reconciled["success_video_ids"] == {"success0001"}


def test_retry_exhausted_queue_rows_are_declared_even_when_not_candidates():
    import pandas as pd
    import channel_crawl

    queue = pd.DataFrame([
        {"video_id": "retry000001", "status": "failed", "retry_count": 3},
        {"video_id": "claim000001", "status": "queued", "retry_count": 0},
    ])
    report = {}
    channel_crawl._record_inventory_queue_scope(report, queue, max_retries=3)
    assert report["queue_scope_video_ids"] == ["claim000001", "retry000001"]
    assert report["retry_exhausted_video_ids"] == ["retry000001"]
    reconciled = reconcile_inventory_scope(
        report["queue_scope_video_ids"], {report["retry_exhausted_video_ids"][0]: "retry_exhausted"}
    )
    assert reconciled["failed"] == 1
    assert reconciled["unresolved"] == 1


def test_unknown_queue_status_is_preserved_as_unresolved_coverage():
    import pandas as pd
    import channel_crawl

    queue = pd.DataFrame([
        {"video_id": "blank000001", "status": "", "retry_count": 0},
        {"video_id": "future00001", "status": "future_status", "retry_count": 0},
        {"video_id": "claim000001", "status": "claimed", "retry_count": 0},
        {"video_id": "done0000001", "status": "done", "retry_count": 0},
    ])
    report = {}
    channel_crawl._record_inventory_queue_scope(report, queue, max_retries=3)
    assert report["queue_scope_video_ids"] == [
        "blank000001", "claim000001", "done0000001", "future00001",
    ]
    reconciled = reconcile_inventory_scope(report["queue_scope_video_ids"], {})
    assert reconciled["unresolved"] == 4
    assert reconciled["succeeded"] + reconciled["failed"] + reconciled["unresolved"] == reconciled["scope_count"]

    resolved = {}
    channel_crawl._record_inventory_queue_scope(
        resolved, queue, max_retries=3,
        output_df=pd.DataFrame([{"v_id": "done0000001", "status": "success"}]),
    )
    terminal_counts = reconcile_inventory_scope(
        resolved["queue_scope_video_ids"], resolved["queue_scope_outcomes"],
    )
    assert terminal_counts["manifest_succeeded"] == 1
    assert terminal_counts["unresolved"] == 3


def test_coverage_state_readers_reject_missing_required_columns(tmp_path):
    import main
    import manual_video_input
    import processed_video_registry
    import channel_crawl

    data = tmp_path / "data"
    data.mkdir()
    (data / "output_df_new.csv").write_text("date,url,v_id\n", encoding="utf-8-sig")
    with pytest.raises(channel_crawl.CoverageStateReadError, match="output_df missing required columns"):
        main.load_output_df_only(str(data))

    (data / "input_df.csv").write_text("url\nhttps://youtu.be/manual00001\n", encoding="cp949")
    with pytest.raises(channel_crawl.CoverageStateReadError, match="input_df missing required columns"):
        main.get_input_urls_for_channel_crawl(str(data), None)

    (data / channel_crawl.CRAWL_QUEUE_FILENAME).write_text("video_id,status\nvideo000001,queued\n", encoding="utf-8-sig")
    with pytest.raises(channel_crawl.CoverageStateReadError, match="crawl queue missing required columns"):
        channel_crawl.load_crawl_queue_df(str(data))

    (data / "channel_df.csv").write_text("channel_url\nhttps://youtube.test/channel\n", encoding="utf-8-sig")
    with pytest.raises(channel_crawl.CoverageStateReadError, match="channel_df missing required columns"):
        channel_crawl.load_channel_df(str(data))

    (data / "manual_input_status.csv").write_text("video_id,status\nmanual00001,new\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="manual ledger missing required columns"):
        manual_video_input.read_ledger(data / "manual_input_status.csv", require_schema=True)

    (data / processed_video_registry.REGISTRY_NAME).write_text("video_id,last_status\nvideo000001,success\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="processed registry missing required columns"):
        processed_video_registry.read_registry(processed_video_registry.registry_path(data))


def test_inventory_completion_gate_exhaustively_rejects_fault_injections():
    """Fault each persisted source, Drive checkpoint, and interruption boundary."""
    from itertools import product

    state_files = ("queue", "output_df", "input_df", "ledger", "channel_df", "processed_registry")
    faults = []
    for state_file in state_files:
        for condition in ("missing", "unreadable", "missing_columns"):
            faults.append((f"{state_file}:{condition}", "state"))
        faults.append((f"{state_file}:unknown_status", "status"))
    for step in (
        "inventory_start_write", "manifest_write", "manifest_readback", "inventory_create",
        "inventory_update", "inventory_metadata_read", "inventory_file_readback",
        "inventory_identity_readback",
    ):
        faults.append((f"drive:{step}", "drive"))
    for point in ("before_batch_complete", "during_manifest_publish", "during_inventory_publish"):
        faults.append((f"interrupt:{point}", "interrupt"))

    def complete_record():
        return {
            "batch_completed": True,
            "discovery_complete": True,
            "coverage_state_valid": True,
            "count_partition_verified": True,
            "source_counts": {"succeeded": 1, "failed": 0, "unresolved": 0, "scope_count": 1},
        }

    for label, kind in faults:
        record = complete_record()
        manifest_verified = True
        inventory_file_verified = True
        all_successes_present = True
        if kind == "state":
            record["coverage_state_valid"] = False
        elif kind == "status":
            assert classify_inventory_status("future_status") == "unresolved"
            record["source_counts"].update({"succeeded": 0, "unresolved": 1, "scope_count": 1})
        elif kind == "drive":
            if label.endswith(("manifest_write", "manifest_readback")):
                manifest_verified = False
            elif label.endswith("inventory_start_write"):
                record["coverage_state_valid"] = False
            else:
                inventory_file_verified = False
        else:
            if label.endswith("before_batch_complete"):
                record["batch_completed"] = False
            elif label.endswith("during_manifest_publish"):
                manifest_verified = False
            else:
                inventory_file_verified = False
        decision = inventory_completion_decision(
            record,
            manifest_readback_verified=manifest_verified,
            inventory_file_readback_verified=inventory_file_verified,
            all_successes_present=all_successes_present,
        )
        assert decision["inventory_complete"] is False, label

    # Exhaust every combination of independent positive evidence flags too.
    for batch, discovery, state, partition, manifest, inventory_file, successes in product((False, True), repeat=7):
        record = complete_record()
        record.update({
            "batch_completed": batch,
            "discovery_complete": discovery,
            "coverage_state_valid": state,
            "count_partition_verified": partition,
        })
        decision = inventory_completion_decision(
            record,
            manifest_readback_verified=manifest,
            inventory_file_readback_verified=inventory_file,
            all_successes_present=successes,
        )
        assert decision["inventory_complete"] is all((batch, discovery, state, partition, manifest, inventory_file, successes))

    for counts in (
        {"succeeded": 1, "failed": 0, "unresolved": 0, "scope_count": 2, "manifest_succeeded": 1},
        {"succeeded": 0, "failed": 1, "unresolved": 0, "scope_count": 1, "manifest_succeeded": 0},
        {"succeeded": 0, "failed": 0, "unresolved": 1, "scope_count": 1, "manifest_succeeded": 0},
        {"succeeded": 0, "failed": 0, "unresolved": 0, "scope_count": 0, "manifest_succeeded": 1},
    ):
        record = complete_record()
        record["source_counts"] = counts
        decision = inventory_completion_decision(
            record,
            manifest_readback_verified=True,
            inventory_file_readback_verified=True,
            all_successes_present=True,
        )
        assert decision["inventory_complete"] is False


@pytest.mark.parametrize(
    ("failed_state", "expected_reason"),
    [
        ("queue", "crawl_queue_read_failed"),
        ("output", "output_df_read_failed"),
        ("channel", "channel_df_read_failed"),
    ],
)
def test_unreadable_coverage_state_keeps_inventory_incomplete_and_queue_intact(
        tmp_path, monkeypatch, failed_state, expected_reason):
    """Exercise coverage reads with a pending queue row behind the discovery cursor."""
    import pandas as pd
    import main
    import channel_crawl

    data = tmp_path / "data"
    data.mkdir()
    base, md = tmp_path / "base", tmp_path / "md"
    base.mkdir()
    md.mkdir()
    cursor = "2026-10-22T00:00:00Z"
    (data / "channel_df.csv").write_text(
        "channel_url,channel_name,usage_channel,channel_id,uploads_playlist_id,last_processed_published_at,last_discovered_published_at,auto_sub_only\n"
        f"https://www.youtube.com/channel/UC{'a' * 22},Fixture,fixture,UC{'a' * 22},UU{'a' * 22},{cursor},{cursor},\n",
        encoding="utf-8-sig",
    )
    (data / "output_df_new.csv").write_text("date,url,v_id,status\n", encoding="utf-8-sig")
    queue_path = data / channel_crawl.CRAWL_QUEUE_FILENAME
    pd.DataFrame([{
        **{column: "" for column in channel_crawl.CRAWL_QUEUE_COLUMNS},
        "video_id": "pending00001",
        "url": "https://youtu.be/pending00001",
        "channel_id": "UC" + "a" * 22,
        "published_at": "2026-10-20T00:00:00Z",
        "status": "queued",
        "retry_count": 0,
    }]).to_csv(queue_path, index=False)
    original_queue_bytes = queue_path.read_bytes()
    original_read_csv = pd.read_csv

    _install_fake_media(monkeypatch)
    sink = DriveApiSink(_FakeDrive())
    monkeypatch.setattr(main, "initialize_clients", lambda _config: (None, None, None))
    monkeypatch.setattr(main, "_run_batch_cleanup", lambda *_args, **_kwargs: None)
    if failed_state == "queue":
        def fail_queue_read(path, *args, **kwargs):
            if str(path) == str(queue_path):
                raise OSError("injected queue read failure")
            return original_read_csv(path, *args, **kwargs)

        monkeypatch.setattr(pd, "read_csv", fail_queue_read)
    elif failed_state == "output":
        monkeypatch.setattr(main, "load_output_df_only", lambda *_args: (_ for _ in ()).throw(
            OSError("injected output_df read failure")))
    else:
        monkeypatch.setattr(channel_crawl, "read_channel_df_raw", lambda *_args: (_ for _ in ()).throw(
            OSError("injected channel_df read failure")))
    monkeypatch.setenv("P03_DRIVE_BACKEND", "api")
    monkeypatch.setenv("P03_DRIVE_SYNC_ENABLED", "0")
    monkeypatch.delenv("P03_SHEET_IMPORT_ENABLED", raising=False)
    config = {
        "BASE_PATH": str(base), "DATA_ROOT": str(data), "WORK_PATH": str(base),
        "HF_HOME": str(tmp_path / "hf"), "OUTPUT_MD_PATH": str(md),
        "CHANNEL_CRAWL": True, "YOUTUBE_API_KEY": "fixture",
        "FILTERING_SHORTS_MINUTES": 0, "CRAWL_QUEUE_MAX_RETRIES": 3,
        "DRIVE_API_SINK": sink,
    }

    if failed_state == "output":
        with pytest.raises(OSError, match="injected output_df read failure"):
            main.process_videos(config)
    else:
        main.process_videos(config)

    inventory = sink.read_manifest()["inventory"]
    assert inventory["inventory_complete"] is False
    assert inventory["discovery_complete"] is False
    assert inventory["source_counts"]["unresolved"] >= 1
    assert expected_reason in inventory["incomplete_reason"]
    assert queue_path.read_bytes() == original_queue_bytes
    assert sink.read_inventory_evidence() is None


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


def test_unresolved_configured_channel_survives_and_recovers_on_next_run(tmp_path, monkeypatch):
    import csv
    import pandas as pd
    import channel_crawl

    channel_url = "https://www.youtube.com/@recover_later"
    channel_id = "UC" + "r" * 22
    video_id = "recover1234"
    channel_file = tmp_path / channel_crawl.CHANNEL_DF_FILENAME
    with channel_file.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(channel_crawl.CHANNEL_DF_COLUMNS[:-1])
        writer.writerow([channel_url, "Recovery", "", "", "", "2026-10-22T00:00:00Z", "", ""])

    resolved = {"value": None}
    monkeypatch.setattr(channel_crawl, "extract_channel_id_from_url", lambda _url: resolved["value"])
    monkeypatch.setattr(channel_crawl, "fetch_channel_via_api", lambda _key, cid, **_kwargs: (
        [{"video_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}",
          "published_at": "2026-10-23T00:00:00Z", "channel_id": cid}],
        "Recovery", "UU" + "r" * 22,
    ))
    config = {"YOUTUBE_API_KEY": "synthetic", "FILTERING_SHORTS_MINUTES": 0}
    output = pd.DataFrame(columns=["v_id"])

    first_report = {}
    channel_crawl.build_queue_and_get_candidates(
        str(tmp_path), config, output, discovery_report=first_report,
    )
    assert first_report["channel_count"] == 1
    assert first_report["complete"] is False
    assert first_report["unresolved"] == 1
    assert first_report["unresolved_channels"] == [
        {"channel_url": channel_url, "reason": "channel_id_unresolved"}
    ]
    assert len(channel_crawl.load_channel_df(str(tmp_path))) == 1
    assert channel_crawl.load_channel_df(str(tmp_path))[0]["channel_id"] == ""

    resolved["value"] = channel_id
    second_report = {}
    channel_crawl.build_queue_and_get_candidates(
        str(tmp_path), config, output, discovery_report=second_report,
    )
    assert second_report["channel_count"] == 1
    assert second_report["discovery_complete"] is True
    assert second_report["complete"] is False  # recovered video is queued, not processed yet
    queue = channel_crawl.load_crawl_queue_df(str(tmp_path))
    assert video_id in set(queue["video_id"].astype(str))
