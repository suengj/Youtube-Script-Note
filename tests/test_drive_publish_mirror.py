# -*- coding: utf-8 -*-
"""SUE-1298: Drive Desktop canonical write + selective vault mirror (fake mounts only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.drive_yt_summary import publish as pub  # noqa: E402
from scripts.drive_yt_summary.publish import flush_staging, publish_final_md  # noqa: E402

NAME = "채널_영상abc_ko_5-mini.md"
REL = f"2026_10_06/{NAME}"
CONTENT = "---\nformat_version: 4.1\ntitle: 제목\n---\n\n# 제목\n\n본문 요약\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    drive = tmp_path / "YT_summary"
    (drive / "source").mkdir(parents=True)
    vault = tmp_path / "vault"
    vault.mkdir()
    base = tmp_path / "base"
    work = tmp_path / "work"
    base.mkdir()
    work.mkdir()
    monkeypatch.delenv("P03_DRIVE_SYNC_ENABLED", raising=False)

    def stage(content=CONTENT, rel=REL):
        p = pub.staging_root(str(work), str(base)) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8-sig")
        return p

    def run(mirror=False, content=CONTENT, rel=REL, sync_root=None, staging=True):
        st = stage(content, rel) if staging else None
        return publish_final_md(
            rel_path=rel, content=content, staging_path=str(st) if st else None,
            md_root=str(vault), mirror=mirror, base_path=str(base), work_path=str(work),
            sync_root=str(sync_root or drive), video_id="abc", title="제목",
        ), st

    return type("E", (), dict(drive=drive, vault=vault, base=base, work=work, run=staticmethod(run), stage=staticmethod(stage)))


def test_adapter_contract_korean_filename(env, monkeypatch):
    monkeypatch.setenv("P03_SELECTIVE_MIRROR", "1")
    res, st = env.run()
    dest = env.drive / "source" / NAME
    assert res.drive_action == "created" and res.revision == 1
    assert dest.read_text(encoding="utf-8") == CONTENT  # content unchanged
    assert not st.exists() and not res.staging_retained  # dropped after verified write
    assert res.mirror_action == "skipped"
    assert not (env.vault / REL).exists()  # unselected stays out of vault


def test_marker_subset_mirrors_only_on(env, monkeypatch):
    monkeypatch.setenv("P03_SELECTIVE_MIRROR", "1")
    on, _ = env.run(mirror=True, rel="2026_10_06/on_a.md")
    off, _ = env.run(mirror=False, rel="2026_10_06/off_b.md")
    assert on.mirror_action == "mirrored"
    assert (env.vault / "2026_10_06/on_a.md").read_text(encoding="utf-8-sig") == CONTENT
    assert not (env.vault / "2026_10_06/off_b.md").exists()
    assert (env.drive / "source/on_a.md").is_file() and (env.drive / "source/off_b.md").is_file()


def test_staging_retained_on_drive_failure_and_flush_retries(env, tmp_path):
    res, st = env.run(sync_root=tmp_path / "missing" / "YT_summary")
    assert res.drive_action == "failed" and res.staging_retained and st.is_file()
    # mount returns: retry from staging
    out = flush_staging(md_root=str(env.vault), base_path=str(env.base),
                        work_path=str(env.work), sync_root=str(env.drive))
    assert [r.drive_action for r in out] == ["created"]
    assert (env.drive / "source" / NAME).is_file() and not st.exists()


def test_mirror_failure_does_not_fail_drive(env, monkeypatch):
    monkeypatch.setenv("P03_SELECTIVE_MIRROR", "1")
    (env.vault / "2026_10_06").write_text("i am a file, not a dir")  # blocks mkdir
    res, st = env.run(mirror=True)
    assert res.mirror_action == "failed"
    assert res.drive_ok and (env.drive / "source" / NAME).is_file()
    assert any(e.startswith("mirror:") for e in res.errors)


def test_drive_failure_does_not_block_mirror(env, tmp_path, monkeypatch):
    monkeypatch.setenv("P03_SELECTIVE_MIRROR", "1")
    res, st = env.run(mirror=True, sync_root=tmp_path / "nope")
    assert res.drive_action == "failed" and res.mirror_action == "mirrored"
    assert st.is_file()  # still retained: Drive not verified


def test_rerun_no_duplicate_and_change_is_revision(env):
    env.run()
    again, _ = env.run()
    assert again.drive_action == "unchanged"
    changed, _ = env.run(content=CONTENT + "\n수정\n")
    assert changed.drive_action == "updated" and changed.revision == 2
    assert len(list((env.drive / "source").glob("*.md"))) == 1
    state = json.loads(next(env.work.rglob("drive_yt_summary_sync_state.json")).read_text(encoding="utf-8"))
    e = state["files"][REL]
    assert e["video_id"] == "abc" and e["revision"] == 2 and len(e["content_hash"]) == 64


def test_batch_sync_after_publish_is_noop_and_manifest_has_ids(env):
    from scripts.drive_yt_summary.sync import run_sync

    env.run(mirror=True)
    r = run_sync(dry_run=False, migrate_legacy=False, md_path=str(env.vault),
                 sync_root=str(env.drive), base_path=str(env.base), work_path=str(env.work))
    assert r.created == 0 and r.updated == 0 and r.errors == 0
    manifest = (env.drive / "manifest.yaml").read_text(encoding="utf-8")
    assert "video_id: abc" in manifest and "hash:" in manifest


def test_disabled_falls_back_to_vault(env, monkeypatch):
    monkeypatch.setenv("P03_DRIVE_SYNC_ENABLED", "0")
    res, st = env.run(mirror=False)
    assert res.drive_action == "disabled" and res.mirror_action == "mirrored"
    assert not list((env.drive / "source").glob("*.md")) and not st.exists()


def test_flag_off_mirrors_unmarked_channel(env):
    res, _ = env.run(mirror=False)  # default: P03_SELECTIVE_MIRROR off
    assert res.drive_ok and res.mirror_action == "mirrored"
    assert (env.vault / REL).is_file()


def test_flag_off_unmarked_is_catalogued_path(env):
    # main.py catalogues whenever mirror_action in (mirrored, unchanged)
    env.run(mirror=False)
    again, _ = env.run(mirror=False)
    assert again.mirror_action == "unchanged"


def test_never_mirror_keeps_failed_drive_publish_out_of_vault(env, tmp_path, monkeypatch):
    monkeypatch.delenv("P03_SELECTIVE_MIRROR", raising=False)
    # Exercise the real publish function directly with its hard no-mirror guard.
    st = env.stage(rel="legacy_import/bromath.md")
    res = publish_final_md(
        rel_path="legacy_import/bromath.md", content=CONTENT, staging_path=str(st),
        md_root=str(env.vault), mirror=False, base_path=str(env.base), work_path=str(env.work),
        sync_root=str(tmp_path / "missing" / "YT_summary"), video_id="abcdefghijk",
        never_mirror=True,
    )
    assert res.drive_action == "failed" and res.staging_retained
    assert list(env.vault.rglob("*.md")) == []
    assert st.is_file()


def test_flag_on_unmarked_not_mirrored(env, monkeypatch):
    monkeypatch.setenv("P03_SELECTIVE_MIRROR", "1")
    res, _ = env.run(mirror=False)
    assert res.drive_ok and res.mirror_action == "skipped"
    assert not (env.vault / REL).exists()


def test_drive_config_error_still_mirrors_when_selective(env, tmp_path, monkeypatch):
    monkeypatch.setenv("P03_SELECTIVE_MIRROR", "1")
    from scripts.drive_yt_summary import config as cfg

    def boom(*a, **k):
        raise cfg.DriveSyncConfigError("no root")

    monkeypatch.setattr(pub, "load_config", boom)
    res, st = env.run(mirror=False)
    assert res.drive_action == "failed" and res.mirror_action == "mirrored"
    assert (env.vault / REL).is_file() and st.is_file()


def test_corrupting_write_fails_readback_and_keeps_staging(env, monkeypatch):
    def corrupt(path, content):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content[:-5], encoding="utf-8")

    monkeypatch.setattr(pub, "atomic_write_text", corrupt)
    res, st = env.run(mirror=False)
    assert res.drive_action == "failed" and any("verify failed" in e for e in res.errors)
    assert res.staging_retained and st.is_file()
