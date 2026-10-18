from __future__ import annotations

import io
import json
import tarfile
from datetime import datetime, timezone

import pytest

import p03_state_backup as backup
from drive_api_sink import DriveUploadError


def _state(tmp_path):
    data = tmp_path / "data"
    base = tmp_path / "base"
    (data / "logs").mkdir(parents=True)
    (base / "index").mkdir(parents=True)
    (data / "channel_df.csv").write_text("channel\n", encoding="utf-8")
    (data / "logs" / "llm_usage.jsonl").write_text('{"tokens":1}\n', encoding="utf-8")
    (base / "index" / "drive_yt_summary_sync_state.json").write_text("{}", encoding="utf-8")
    return data, base


def test_source_identity_uses_only_canonical_state_paths_and_manifest_hashes(tmp_path):
    data, base = _state(tmp_path)
    name, packed = backup.build_archive(str(data), str(base), now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert name == "p03-state-20260102T000000Z.tar.gz"
    backup.validate_archive(packed)
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as archive:
        entries = {item.name: archive.extractfile(item).read() for item in archive.getmembers() if item.name != "manifest.json"}
    assert set(entries) == {"data/channel_df.csv", "data/logs/llm_usage.jsonl", "index/drive_yt_summary_sync_state.json"}


def test_symlink_substitution_and_toctou_change_fail_closed(tmp_path, monkeypatch):
    data, base = _state(tmp_path)
    outside = tmp_path / "outside.csv"
    outside.write_text("private", encoding="utf-8")
    target = data / "channel_df.csv"
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(DriveUploadError):
        backup.build_archive(str(data), str(base))

    target.unlink()
    target.write_text("ok", encoding="utf-8")
    original_read = backup.os.read
    changed = False

    def race(fd, size):
        nonlocal changed
        if not changed:
            changed = True
            target.write_text("replacement", encoding="utf-8")
        return original_read(fd, size)

    monkeypatch.setattr(backup.os, "read", race)
    with pytest.raises(DriveUploadError, match="changed while reading"):
        backup.build_archive(str(data), str(base))


def test_nested_or_partial_manifest_member_fails(tmp_path):
    data, base = _state(tmp_path)
    packed = backup.build_archive(str(data), str(base))[1]
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as archive:
        entries = {m.name: archive.extractfile(m).read() for m in archive.getmembers()}
    manifest = json.loads(entries["manifest.json"])
    manifest["files"][0]["size"] += 1
    entries["manifest.json"] = json.dumps(manifest).encode()
    altered = io.BytesIO()
    with tarfile.open(fileobj=altered, mode="w:gz") as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    with pytest.raises(DriveUploadError):
        backup.validate_archive(altered.getvalue())

    with pytest.raises(DriveUploadError):
        # Malformed gzip input also fails with the same typed error.
        backup.validate_archive(b"not a gzip archive")

    entries["manifest.json"] = json.dumps({"schema_version": 1, "files": []}).encode()
    entries["../../outside"] = b"escaped"
    malformed = io.BytesIO()
    with tarfile.open(fileobj=malformed, mode="w:gz") as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    with pytest.raises(DriveUploadError):
        backup.validate_archive(malformed.getvalue())


def test_missing_and_oversized_state_fail_closed(tmp_path):
    data, base = _state(tmp_path)
    for relative, root in backup.STATE_FILES:
        path = (data / relative.split("/", 1)[1]) if root == "data" else (base / relative)
        if path.exists():
            path.unlink()
    with pytest.raises(DriveUploadError, match="no P03 state"):
        backup.build_archive(str(data), str(base))
    oversized = data / "channel_df.csv"
    oversized.write_bytes(b"x" * (backup.MAX_FILE_BYTES + 1))
    with pytest.raises(DriveUploadError, match="bounded regular file"):
        backup.build_archive(str(data), str(base))


def test_backup_failure_does_not_mutate_local_state_or_prune(tmp_path):
    data, base = _state(tmp_path)
    before = {str(p): p.read_bytes() for p in list(data.rglob("*")) + list(base.rglob("*")) if p.is_file()}

    class FakeDrive:
        def __init__(self):
            self.deleted = []
        def _state_backup_folder(self):
            return "fake-folder"
        def _upload_state_backup(self, *_args):
            raise DriveUploadError("readback mismatch")
        def _prune_state_backups(self, *args, **kwargs):
            self.deleted.append(args)

    fake = FakeDrive()
    with pytest.raises(DriveUploadError):
        backup.backup_state(fake, str(data), str(base))
    assert not fake.deleted
    assert before == {str(p): p.read_bytes() for p in list(data.rglob("*")) + list(base.rglob("*")) if p.is_file()}


def test_one_archive_upload_is_read_back_with_size_and_md5(tmp_path):
    import hashlib
    from drive_api_sink import DriveApiSink

    data, base = _state(tmp_path)
    class Request:
        def __init__(self, fn): self.fn = fn
        def execute(self): return self.fn()
    class Files:
        def __init__(self): self.items = {}; self.created_archives = 0
        def list(self, **kwargs):
            def run():
                if "name='state-backup'" in kwargs.get("q", ""):
                    folders = [item for item in self.items.values() if item.get("mimeType") == "application/vnd.google-apps.folder"]
                    return {"files": folders}
                parent = "folder" if "'folder' in parents" in kwargs.get("q", "") else "root"
                return {"files": [item for item in self.items.values() if parent in item.get("parents", [])]}
            return Request(run)
        def create(self, *, body, media_body=None, **_kwargs):
            def run():
                fid = "folder" if body.get("mimeType") else "archive"
                if media_body is not None:
                    self.created_archives += 1
                    content = media_body._fd.getvalue()
                    self.items[fid] = {**body, "id": fid, "size": str(len(content)),
                        "md5Checksum": hashlib.md5(content).hexdigest(), "parents": body["parents"], "content": content}
                else:
                    self.items[fid] = {**body, "id": fid, "parents": body["parents"]}
                return {"id": fid}
            return Request(run)
        def get(self, *, fileId, **_kwargs):
            return Request(lambda: {k: v for k, v in self.items[fileId].items() if k != "content"})
        def delete(self, **_kwargs): return Request(lambda: {})
    class Service:
        def __init__(self): self.api = Files()
        def files(self): return self.api

    service = Service()
    sink = DriveApiSink(service, summary_folder_id="root")
    result = backup.backup_state(sink, str(data), str(base))
    assert result == "archive"
    assert service.api.created_archives == 1
    uploaded = service.api.items["archive"]["content"]
    backup.validate_archive(uploaded)
    assert service.api.items["archive"]["size"] == str(len(uploaded))
    assert service.api.items["archive"]["md5Checksum"] == hashlib.md5(uploaded).hexdigest()


def test_retention_only_considers_tagged_own_backup_records():
    class Files:
        def __init__(self):
            self.deleted = []
        def list(self, **_kwargs):
            class Call:
                def execute(inner):
                    return {"files": [
                        {"id": f"own{i}", "name": f"p03-state-202601{i:02d}T000000Z.tar.gz", "parents": ["folder"], "appProperties": {"p03_kind": "state_backup"}}
                        for i in range(1, 17)
                    ] + [{"id": "foreign", "name": "p03-state-20250101T000000Z.tar.gz", "parents": ["folder"], "appProperties": {"p03_kind": "other"}}]}
            return Call()
        def delete(self, **kwargs):
            self.deleted.append(kwargs["fileId"])
            class Call:
                def execute(inner): return {}
            return Call()
    class Service:
        def __init__(self): self.files_api = Files()
        def files(self): return self.files_api

    sink = object.__new__(__import__("drive_api_sink").DriveApiSink)
    sink._drive = Service()
    sink._prune_state_backups("folder", keep=14, newest_id="own16")
    assert sink._drive.files_api.deleted == ["own2", "own1"]
    assert "foreign" not in sink._drive.files_api.deleted


def test_not_queued_refusal_is_visible_exit_two_and_skips_backup(monkeypatch, capsys, tmp_path):
    import main

    monkeypatch.setattr(main, "load_config", lambda: {"BASE_PATH": str(tmp_path), "CHANNEL_CRAWL": False})
    monkeypatch.setattr(main.run_lock, "acquire_run_lock", lambda *_args: (True, None, ""))
    monkeypatch.setattr(main, "process_videos", lambda _cfg: (_ for _ in ()).throw(main.VideoNotQueuedError("not queued")))
    monkeypatch.setattr(main, "backup_state", lambda *_args: pytest.fail("refused invocation must not write Drive"))
    monkeypatch.setenv("P03_STATE_BACKUP", "1")
    with pytest.raises(SystemExit) as result:
        main.main(["--video-id", "abcdefghijk"])
    assert result.value.code == 2
    assert "VIDEO_NOT_QUEUED abcdefghijk: already done or unknown" in capsys.readouterr().err
