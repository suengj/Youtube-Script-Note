import hashlib
import json
import os
import time

import pytest

import main
from scripts.jev_classify import drive as jd
from scripts.jev_classify.index import empty_index, serialize
from scripts.jev_classify.taxonomy import TOPIC_IDS


class _Req:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class _Files:
    def __init__(self, owner): self.owner = owner
    def get(self, fileId, **kwargs):
        return _Req(lambda: dict(self.owner.meta[fileId]))
    def get_media(self, fileId):
        return _Req(lambda: self.owner.data[fileId])


class _Drive:
    def __init__(self, index):
        self.data = {jd.INDEX_FILE_ID: index}
        self.meta = {jd.INDEX_FILE_ID: self._metadata(index, 1)}
        self.writes = 0
    @staticmethod
    def _metadata(data, revision):
        return {"id": jd.INDEX_FILE_ID, "name": jd.INDEX_FILENAME, "mimeType": "application/json",
                "parents": ["folder"], "headRevisionId": str(revision),
                "modifiedTime": str(revision), "md5Checksum": hashlib.md5(data).hexdigest()}
    def files(self): return _Files(self)


class _Provider:
    name = "jev"
    model = "test-model"
    prompt_version = "test-prompt"
    score_semantics = "independent ordinal relevance"
    usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    def classify_detail(self, text):
        self.usage["requests"] += 1
        return {topic: 30 for topic in TOPIC_IDS}, "reason", {}


def _md(vid):
    return (f"---\nsource_url: https://www.youtube.com/watch?v={vid}\n---\nSummary {vid}\n").encode()


def _entry(vid):
    return {"video_id": vid, "source_url": f"https://www.youtube.com/watch?v={vid}",
            "content_hash": "a" * 64, "category_scores": {topic: 1 for topic in TOPIC_IDS},
            "reason": "external", "score_semantics": "independent ordinal relevance", "taxonomy_version": "1.0.0",
            "classifier": "jev", "model": "model", "prompt_version": "prompt",
            "classified_at": "2026-01-01T00:00:00Z", "stale": False, "error": None,
            "cache_key": "b" * 64, "lineage": {}}


def test_uploaded_receipt_from_worker_config_reaches_batch_config():
    batch_config = {}
    worker_config = main._copy_video_config(batch_config)

    main._record_jev_drive_upload(
        worker_config, {"summary": {"id": "drive-summary-id"}}, "note.md", b"summary"
    )

    assert batch_config["_JEV_DRIVE_UPLOADS"] == [
        ("drive-summary-id", "note.md", b"summary")
    ]


@pytest.fixture
def drive_ops(monkeypatch):
    drive = _Drive(serialize(empty_index()))
    backups = []
    def upload(_drive, file_id, name, parent_id, raw, *, create=False, deadline=None):
        assert file_id == jd.INDEX_FILE_ID
        _drive.writes += 1
        _drive.data[file_id] = raw
        _drive.meta[file_id] = _drive._metadata(raw, int(_drive.meta[file_id]["headRevisionId"]) + 1)
        return file_id
    monkeypatch.setattr(jd, "_upload", upload)
    monkeypatch.setattr(jd, "_backup", lambda _d, _m, raw, deadline=None: backups.append(raw))
    return drive, backups


def test_batch_classifies_only_uploaded_notes_and_writes_once(drive_ops):
    drive, backups = drive_ops
    prior_error = _entry("QRSTU123456")
    prior_error.update({"stale": True, "error": "previous failure", "cache_key": None})
    prior_error["lineage"] = {"drive_name": "retry.md", "drive_file_ids": ["f_retry"]}
    initial = empty_index()
    initial["entries"][prior_error["video_id"]] = prior_error
    initial_raw = serialize(initial)
    drive.data[jd.INDEX_FILE_ID] = initial_raw
    drive.meta[jd.INDEX_FILE_ID] = drive._metadata(initial_raw, 1)
    retry_raw = _md(prior_error["video_id"])
    drive.data["f_retry"] = retry_raw
    drive.meta["f_retry"] = {"id": "f_retry", "name": "retry.md"}
    provider = _Provider()
    report = jd._run(drive, [("f1", "one.md", _md("ABCDEFGHIJK")),
                             ("f2", "two.md", _md("LMNOPQRSTUV"))], provider=provider)
    assert report["calls"] == 3
    assert report["ok"] == 3
    assert drive.writes == 1
    assert len(backups) == 1
    saved = json.loads(drive.data[jd.INDEX_FILE_ID])
    assert set(saved["entries"]) == {"ABCDEFGHIJK", "LMNOPQRSTUV", "QRSTU123456"}


def test_concurrent_change_redownloads_and_merges(drive_ops, monkeypatch):
    drive, backups = drive_ops
    initial_meta, initial_raw, initial = jd._download(drive, jd.INDEX_FILE_ID)
    external = dict(initial)
    external["entries"] = {"ZZZZZZZZZZZ": _entry("ZZZZZZZZZZZ")}
    external_raw = serialize(external)
    drive.data[jd.INDEX_FILE_ID] = external_raw
    drive.meta[jd.INDEX_FILE_ID] = drive._metadata(external_raw, 2)
    original_download = jd._download
    seen = []
    def record_download(*args):
        seen.append(1)
        return original_download(*args)
    monkeypatch.setattr(jd, "_download", record_download)
    jd._write_merge(drive, initial_meta, initial_raw, initial,
                    {"ABCDEFGHIJK": _entry("ABCDEFGHIJK")})
    saved = json.loads(drive.data[jd.INDEX_FILE_ID])
    assert "ZZZZZZZZZZZ" in saved["entries"]
    assert "ABCDEFGHIJK" in saved["entries"]
    assert len(seen) >= 2
    assert backups == [external_raw]


def test_readback_sha_mismatch_is_reported(drive_ops, monkeypatch):
    drive, _ = drive_ops
    real_upload = jd._upload
    def altered_upload(d, file_id, name, parent_id, raw, **kwargs):
        result = real_upload(d, file_id, name, parent_id, raw, **kwargs)
        altered = d.data[file_id] + b" "
        d.data[file_id] = altered
        d.meta[file_id] = d._metadata(altered, int(d.meta[file_id]["headRevisionId"]))
        return result
    monkeypatch.setattr(jd, "_upload", altered_upload)
    meta, raw, payload = jd._download(drive, jd.INDEX_FILE_ID)
    with pytest.raises(RuntimeError, match="SHA-256 readback mismatch"):
        jd._write_merge(drive, meta, raw, payload, {"ABCDEFGHIJK": _entry("ABCDEFGHIJK")})


def test_disabled_hook_is_noop(monkeypatch):
    monkeypatch.delenv("P03_JEV_CLASSIFY_ENABLED", raising=False)
    monkeypatch.setattr(jd, "_run", lambda *a, **k: pytest.fail("hook ran while disabled"))
    assert jd.classify_batch([("id", "a.md", _md("ABCDEFGHIJK"))], drive=object()) is None


def test_hook_failure_is_nonfatal(monkeypatch, caplog):
    monkeypatch.setenv("P03_JEV_CLASSIFY_ENABLED", "1")
    monkeypatch.setattr(jd, "_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("failure")))
    assert jd.classify_batch([], drive=object()) is None
    assert "JEV Drive batch failed (non-fatal)" in caplog.text


def test_time_cap_skips_work_after_deadline(drive_ops):
    drive, _ = drive_ops
    report = jd._run(drive, [("f1", "one.md", _md("ABCDEFGHIJK"))], deadline=time.monotonic() - 1,
                     provider=_Provider())
    assert report["calls"] == 0
    assert drive.writes == 0


def test_time_cap_persists_upload_for_next_run_retry(drive_ops):
    drive, _ = drive_ops
    body = _md("ABCDEFGHIJK")
    drive.data["f1"] = body
    drive.meta["f1"] = {"id": "f1", "name": "one.md"}
    first = jd._run(drive, [("f1", "one.md", body)], deadline=time.monotonic() + 60,
                    classify_deadline=time.monotonic() - 1, provider=_Provider())
    assert first["calls"] == 0
    pending = json.loads(drive.data[jd.INDEX_FILE_ID])["entries"]["ABCDEFGHIJK"]
    assert pending["error"] == "Deferred by JEV phase time cap"
    assert pending["lineage"]["drive_file_ids"] == ["f1"]

    provider = _Provider()
    second = jd._run(drive, [], provider=provider)
    assert second["calls"] == 1
    assert json.loads(drive.data[jd.INDEX_FILE_ID])["entries"]["ABCDEFGHIJK"]["error"] is None


def test_slow_retry_read_cannot_start_classification_after_cap(drive_ops, monkeypatch):
    drive, _ = drive_ops
    pending = _entry("ABCDEFGHIJK")
    pending.update({"stale": True, "error": "previous failure", "cache_key": None})
    pending["lineage"] = {"drive_file_ids": ["f1"]}
    index = empty_index()
    index["entries"][pending["video_id"]] = pending
    raw_index = serialize(index)
    drive.data[jd.INDEX_FILE_ID] = raw_index
    drive.meta[jd.INDEX_FILE_ID] = drive._metadata(raw_index, 1)
    drive.data["f1"] = _md("ABCDEFGHIJK")
    now = [0]
    monkeypatch.setattr(jd, "_download", lambda *a, **k: (
        drive.meta[jd.INDEX_FILE_ID], raw_index, index))
    monkeypatch.setattr(jd, "_meta", lambda *a, **k: {"id": "f1", "name": "one.md"})
    def slow_read(*a, **k):
        now[0] = 20
        return drive.data["f1"]
    monkeypatch.setattr(jd, "_execute", slow_read)
    monkeypatch.setattr(jd.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(jd, "_classify_bytes", lambda *a, **k: pytest.fail("classification started after cap"))
    report = jd._run(drive, [], deadline=100, classify_deadline=10, provider=_Provider())
    assert report["calls"] == 0


def test_backfill_dry_run_has_no_provider_calls_or_writes(monkeypatch, drive_ops, capsys):
    drive, _ = drive_ops
    manifest = {"items": [{"file": "one.md", "drive_file_id": "f1", "date": "2026-01-01"}]}
    drive.data["f1"] = _md("ABCDEFGHIJK")
    drive.meta["f1"] = {"id": "f1", "name": "one.md"}
    class Sink:
        def __init__(self, service=None): pass
        def read_manifest(self): return manifest
    monkeypatch.setattr("drive_api_sink.DriveApiSink", Sink)
    report = jd.backfill_drive(dry_run=True, max_calls=10, drive=drive, provider=_Provider())
    assert report["would_call"] == 1
    assert json.loads(capsys.readouterr().out)["already_valid"] == 0
    assert drive.writes == 0


def test_backfill_requires_explicit_call_limit():
    with pytest.raises(ValueError, match="requires --max-calls"):
        jd.backfill_drive(drive=object())


def test_backfill_writes_each_100_source_chunk(monkeypatch, drive_ops):
    drive, _ = drive_ops
    items = []
    for i in range(101):
        vid = f"{i:011d}"
        fid = f"f{i}"
        items.append({"file": f"{i:03d}.md", "drive_file_id": fid,
                      "date": f"2026-01-{i % 28 + 1:02d}"})
        raw = _md(vid)
        drive.data[fid] = raw
        drive.meta[fid] = {"id": fid, "name": f"{i:03d}.md"}
    class Sink:
        def __init__(self, service=None): pass
        def read_manifest(self): return {"items": items}
    monkeypatch.setattr("drive_api_sink.DriveApiSink", Sink)
    counts = []
    real_run = jd._run
    def count_run(d, chunk, **kwargs):
        counts.append(len(chunk))
        return real_run(d, chunk, **kwargs)
    monkeypatch.setattr(jd, "_run", count_run)
    report = jd.backfill_drive(max_calls=101, drive=drive, provider=_Provider())
    assert counts == [100, 1]
    assert report["processed"] == 101


def test_backfill_orders_canonical_manifest_items_oldest_first(monkeypatch, drive_ops):
    drive, _ = drive_ops
    items = [
        {"file": "a.md", "drive_file_id": "fa", "finalized_at": "2026-03-01T00:00:00Z"},
        {"file": "z.md", "drive_file_id": "fz", "finalized_at": "2026-01-01T00:00:00Z"},
        {"file": "m.md", "drive_file_id": "fm", "recovered_at": "2026-02-01T00:00:00Z"},
    ]
    for item, vid in zip(items, ("ABCDEFGHIJK", "LMNOPQRSTUV", "QRSTU123456")):
        drive.data[item["drive_file_id"]] = _md(vid)
        drive.meta[item["drive_file_id"]] = {"id": item["drive_file_id"], "name": item["file"]}

    class Sink:
        def __init__(self, service=None): pass
        def read_manifest(self): return {"items": items}

    monkeypatch.setattr("drive_api_sink.DriveApiSink", Sink)
    ordered = []
    def record_run(_drive, chunk, **kwargs):
        ordered.extend(row[0] for row in chunk)
        return {"calls": len(chunk), "processed_sources": len(chunk)}
    monkeypatch.setattr(jd, "_run", record_run)

    jd.backfill_drive(max_calls=3, drive=drive, provider=_Provider())

    assert ordered == ["fz", "fm", "fa"]


def test_backfill_max_calls_stops_and_persists_partial_chunk(monkeypatch, drive_ops):
    drive, _ = drive_ops
    items = []
    for i in range(3):
        vid, fid, name = f"{i:011d}", f"f{i}", f"{i}.md"
        items.append({"file": name, "drive_file_id": fid, "date": f"2026-01-0{i + 1}"})
        drive.data[fid] = _md(vid)
        drive.meta[fid] = {"id": fid, "name": name}
    class Sink:
        def __init__(self, service=None): pass
        def read_manifest(self): return {"items": items}
    monkeypatch.setattr("drive_api_sink.DriveApiSink", Sink)
    report = jd.backfill_drive(max_calls=1, drive=drive, provider=_Provider())
    assert report["calls"] == 1
    assert report["processed"] == 1
    assert drive.writes == 1
    assert len(json.loads(drive.data[jd.INDEX_FILE_ID])["entries"]) == 1
