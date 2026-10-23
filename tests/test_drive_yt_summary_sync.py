#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for YT_summary filesystem sync (Drive Desktop)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.drive_yt_summary.config import discover_yt_summary_root  # noqa: E402
from scripts.drive_yt_summary.scanner import is_syncable_relative_path, scan_summary_map  # noqa: E402
from scripts.drive_yt_summary import sync as sync_module  # noqa: E402
from scripts.drive_yt_summary.manifest import parse_manifest_yaml, serialize_manifest_yaml  # noqa: E402
from scripts.drive_yt_summary.state import SyncState, SyncStateEntry, load_state, save_state  # noqa: E402
from scripts.drive_yt_summary.sync import run_sync  # noqa: E402


@pytest.fixture(autouse=True)
def desktop_manifest_writer_authority(monkeypatch):
    monkeypatch.setenv("P03_DRIVE_BACKEND", "desktop")
    monkeypatch.setenv("P03_MANIFEST_WRITER", "desktop")


def _md(body: str = "# Title\n\nBody") -> str:
    return f"---\nformat_version: 4.1\ntitle: Title\n---\n\n{body}\n"


@pytest.fixture
def md_root(tmp_path: Path) -> Path:
    root = tmp_path / "obsidian"
    day = root / "2026_08_31"
    day.mkdir(parents=True)
    (root / "digest").mkdir()
    (root / "digest" / "2026_08_31.md").write_text("# digest", encoding="utf-8")
    for i in range(3):
        (day / f"ch_vid{i}abc1234567_ko_5-mini.md").write_text(
            _md(f"# Note {i}\n\nText {i}"),
            encoding="utf-8",
        )
    return root


@pytest.fixture
def work_env(tmp_path: Path) -> tuple[str, str]:
    base = tmp_path / "project"
    work = tmp_path / "work"
    base.mkdir()
    work.mkdir()
    return str(base), str(work)


@pytest.fixture
def drive_root(tmp_path: Path) -> Path:
    root = tmp_path / "YT_summary"
    (root / "source").mkdir(parents=True)
    (root / "legacy").mkdir(parents=True)
    return root


def _run(
    md_root: Path,
    drive_root: Path,
    work_env: tuple[str, str],
    **kwargs,
):
    base, work = work_env
    return run_sync(
        dry_run=False,
        migrate_legacy=False,
        md_path=str(md_root),
        sync_root=str(drive_root),
        base_path=base,
        work_path=work,
        **kwargs,
    )


def test_scanner_skips_digest(md_root: Path) -> None:
    assert is_syncable_relative_path("digest/2026_08_31.md") is False
    found = scan_summary_map(str(md_root))
    assert len(found) == 3
    assert all(not k.startswith("digest/") for k in found)


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = SyncState(
        files={
            "2026_08_31/a.md": SyncStateEntry(
                relative_path="2026_08_31/a.md",
                content_hash="abc",
                dest_path="/tmp/YT_summary/source/a.md",
                drive_name="a.md",
                updated_at="2026-08-31T00:00:00+00:00",
            )
        },
        manifest_path="/tmp/YT_summary/manifest.yaml",
    )
    save_state(path, state)
    loaded = load_state(path)
    assert loaded.manifest_path == "/tmp/YT_summary/manifest.yaml"
    assert loaded.files["2026_08_31/a.md"].dest_path.endswith("a.md")


def test_case_a_initial_upload(md_root: Path, drive_root: Path, work_env: tuple[str, str]) -> None:
    result = _run(md_root, drive_root, work_env, limit=3)
    assert result.created == 3
    assert result.updated == 0
    assert result.errors == 0
    assert len(list((drive_root / "source").glob("*.md"))) == 3


def test_case_b_idempotency(md_root: Path, drive_root: Path, work_env: tuple[str, str]) -> None:
    _run(md_root, drive_root, work_env, limit=3)
    result = _run(md_root, drive_root, work_env, limit=3)
    assert result.created == 0
    assert result.updated == 0
    assert len(list((drive_root / "source").glob("*.md"))) == 3


def test_case_c_update_same_dest(md_root: Path, drive_root: Path, work_env: tuple[str, str]) -> None:
    _run(md_root, drive_root, work_env, limit=3)
    target = list((md_root / "2026_08_31").glob("*.md"))[0]
    dest = drive_root / "source" / target.name
    first_mtime = dest.read_text(encoding="utf-8")
    target.write_text(_md("# Changed\n\nNew body"), encoding="utf-8")
    result = _run(md_root, drive_root, work_env, limit=3)
    assert result.created == 0
    assert result.updated == 1
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") != first_mtime
    assert len(list((drive_root / "source").glob(target.name))) == 1


def test_case_d_local_delete_keeps_dest(md_root: Path, drive_root: Path, work_env: tuple[str, str]) -> None:
    _run(md_root, drive_root, work_env, limit=3)
    target = list((md_root / "2026_08_31").glob("*.md"))[0]
    dest = drive_root / "source" / target.name
    assert dest.is_file()
    target.unlink()
    result = _run(md_root, drive_root, work_env, limit=3)
    assert dest.is_file()
    assert result.created == 0
    assert result.updated == 0


def test_case_e_missing_mount_failure(md_root: Path, work_env: tuple[str, str], tmp_path: Path) -> None:
    missing = tmp_path / "missing_drive" / "YT_summary"
    base, work = work_env
    original = [p.read_text(encoding="utf-8") for p in (md_root / "2026_08_31").glob("*.md")]
    result = run_sync(
        dry_run=False,
        migrate_legacy=False,
        limit=3,
        md_path=str(md_root),
        sync_root=str(missing),
        base_path=base,
        work_path=work,
    )
    assert result.errors >= 1
    after = [p.read_text(encoding="utf-8") for p in (md_root / "2026_08_31").glob("*.md")]
    assert after == original


def test_backfill_date_filter(md_root: Path, drive_root: Path, work_env: tuple[str, str]) -> None:
    other = md_root / "2026_08_30"
    other.mkdir()
    (other / "old.md").write_text(_md("old"), encoding="utf-8")
    result = _run(md_root, drive_root, work_env, backfill_date="2026-08-31")
    assert result.created == 3
    assert not (drive_root / "source" / "old.md").exists()


def test_legacy_migration(drive_root: Path) -> None:
    contents = drive_root / "contents_gen"
    contents.mkdir()
    (contents / "completed_files.txt").write_text("ok", encoding="utf-8")
    from scripts.drive_yt_summary.legacy import migrate_contents_gen_to_legacy

    res = migrate_contents_gen_to_legacy(drive_root, dry_run=False)
    assert res.moved
    assert (drive_root / "legacy" / "contents_gen" / "completed_files.txt").is_file()
    assert not contents.exists()


def test_discover_yt_summary_on_machine() -> None:
    """Smoke: discovery returns a path on operator Mac when Drive Desktop is mounted."""
    path = discover_yt_summary_root()
    if path is not None:
        assert path.name == "YT_summary"
        assert path.is_dir()


def test_sync_refuses_manifest_write_without_explicit_desktop_authority(md_root, drive_root, work_env, monkeypatch):
    monkeypatch.delenv("P03_MANIFEST_WRITER", raising=False)
    result = _run(md_root, drive_root, work_env, limit=1)
    assert result.errors == 1
    assert "P03_MANIFEST_WRITER=desktop" in result.error_messages[0]
    assert not (drive_root / "manifest.yaml").exists()
    assert not list((drive_root / "source").glob("*.md"))


def test_sync_refuses_even_explicit_desktop_authority_when_backend_is_api(md_root, drive_root, work_env, monkeypatch):
    monkeypatch.setenv("P03_DRIVE_BACKEND", "api")
    result = _run(md_root, drive_root, work_env, limit=1)
    assert result.errors == 1
    assert "DriveApiSink" in result.error_messages[0]
    assert not (drive_root / "manifest.yaml").exists()


def test_sync_requires_explicit_authority_for_inventory_or_lifecycle_manifest(
    md_root, drive_root, work_env, monkeypatch
):
    monkeypatch.delenv("P03_MANIFEST_WRITER", raising=False)
    manifest_path = drive_root / "manifest.yaml"
    before = serialize_manifest_yaml({
        "version": 1, "inventory": {"inventory_complete": False},
        "items": [{"file": "old.md", "finalized_at": "2026-10-20T00:00:00Z"}],
    })
    manifest_path.write_bytes(before)
    result = _run(md_root, drive_root, work_env, limit=1)
    assert result.errors == 1
    assert "inventory/lifecycle fields" in result.error_messages[0]
    assert manifest_path.read_bytes() == before


def test_desktop_sync_merges_without_dropping_api_inventory_or_lifecycle_fields(
    md_root, drive_root, work_env
):
    old_item = {
        "file": "api_only_abcdefghijk.md", "title": "API only", "hash": "a" * 64,
        "revision": 3, "video_id": "abcdefghijk", "drive_file_id": "drive-source-1",
        "finalized_at": "2026-10-20T00:00:00Z", "custom_field": {"keep": True},
    }
    inventory = {"inventory_revision": 4, "inventory_complete": False, "unknown": "preserve"}
    manifest_path = drive_root / "manifest.yaml"
    manifest_path.write_bytes(serialize_manifest_yaml({
        "version": 1, "producer_extension": {"keep": True}, "inventory": inventory,
        "items": [old_item],
    }))

    result = _run(md_root, drive_root, work_env, limit=1)

    assert result.errors == 0
    merged = parse_manifest_yaml(manifest_path.read_bytes())
    assert merged["producer_extension"] == {"keep": True}
    assert merged["inventory"] == inventory
    assert old_item in merged["items"]
    assert len(merged["items"]) == 2


def test_manifest_merge_retry_restores_item_removed_by_racing_writer(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.yaml"
    retained = {"file": "api_only.md", "title": "API", "hash": "a" * 64, "revision": 2,
                "video_id": "abcdefghijk", "drive_file_id": "drive-api-item", "unknown": "keep"}
    manifest_path.write_bytes(serialize_manifest_yaml({
        "version": 1, "inventory": {"keep": True}, "items": [retained],
    }))
    entry = SyncStateEntry(
        relative_path="new/new.md", content_hash="b" * 64, dest_path="/drive/new.md",
        drive_name="new.md", updated_at="2026-10-23T00:00:00Z", video_id="lmnopqrst",
        title="New", revision=1,
    )
    real_write = sync_module.atomic_write_text
    calls = 0

    def clobber_once(path, content):
        nonlocal calls
        calls += 1
        real_write(path, content)
        if calls == 1:
            doc = parse_manifest_yaml(Path(path).read_bytes())
            doc["items"] = [item for item in doc["items"] if item.get("drive_file_id") != "drive-api-item"]
            real_write(path, serialize_manifest_yaml(doc).decode("utf-8"))

    monkeypatch.setattr(sync_module, "atomic_write_text", clobber_once)
    sync_module._write_merged_manifest(manifest_path, {"new/new.md": entry}, {"new/new.md": "New"})

    final = parse_manifest_yaml(manifest_path.read_bytes())
    assert retained in final["items"]
    assert any(item.get("video_id") == "lmnopqrst" for item in final["items"])
    assert final["inventory"] == {"keep": True}
    assert calls == 2
