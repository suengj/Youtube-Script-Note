"""SUE-1327: P03 JEV classify-if-needed, retry, invalidation, migration and publish hook (fake provider)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.jev_classify import classify as cl
from scripts.jev_classify import index as ix
from scripts.jev_classify import migrate as mg
from scripts.jev_classify.__main__ import main as cli_main
from scripts.jev_classify.taxonomy import TOPIC_IDS

VID = "p8rphwT9g-A"


def md(vid=VID, body="본문 요약", url=True):
    head = "---\nformat_version: '4.1'\n"
    head += f"vid: {vid}\n"
    if url:
        head += f"source_url: https://www.youtube.com/watch?v={vid}\n"
    return head + "title: 제목\ntldr: 짧은 요약\n---\n\n# 제목\n\n" + body + "\n"


class FakeProvider:
    name = "jev"
    model = "jev-1.13.0"
    score_semantics = "uncalibrated_relevance: fake"

    def __init__(self, prompt_version="jev-rubric-b03a101ddb34", fail=False):
        self.prompt_version = prompt_version
        self.fail = fail
        self.calls = 0

    def classify_detail(self, text):
        self.calls += 1
        if self.fail:
            raise RuntimeError("JEV HTTP status 503")
        scores = {t: 0 for t in TOPIC_IDS}
        scores["ai_tech"] = 75
        return scores, "ai_tech L3: Major theme", {"categories": {"ai_tech": {"confidence": 0.9}}}


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "YT_summary"
    (root / "source").mkdir(parents=True)
    src = root / "source" / f"채널_제목_{VID}_ko_luna-low.md"
    src.write_text(md(), encoding="utf-8")
    manifest = root / "manifest.yaml"
    manifest.write_text("version: 1\nitems: []\n", encoding="utf-8")
    return type("E", (), dict(root=root, src=src, index=root / ix.INDEX_FILENAME, manifest=manifest))


def run(env, provider, **kw):
    return cl.classify_if_needed(env.src, env.index, provider=provider, now=lambda: "2026-10-06T00:00:00Z", **kw)


def entries(env):
    return json.loads(env.index.read_text(encoding="utf-8"))["entries"]


def test_new_source_one_call_rerun_zero_change_one(env):
    p = FakeProvider()
    assert run(env, p).action == "classified" and p.calls == 1
    entry = entries(env)[VID]
    assert entry["category_scores"]["ai_tech"] == 75 and entry["error"] is None
    assert entry["content_hash"] == hashlib.sha256(env.src.read_bytes()).hexdigest()
    assert entry["lineage"]["drive_name"] == env.src.name
    ix.validate(json.loads(env.index.read_text(encoding="utf-8")))

    before = env.index.read_bytes()
    out = run(env, p)
    assert out.action == "cached" and out.provider_calls == 0 and p.calls == 1
    assert env.index.read_bytes() == before  # unchanged source: no call, no rewrite

    env.src.write_text(md(body="수정된 본문"), encoding="utf-8")
    assert run(env, p).action == "classified" and p.calls == 2


@pytest.mark.parametrize("bump", ["model", "prompt", "taxonomy"])
def test_model_rubric_taxonomy_bump_invalidates(env, bump):
    run(env, FakeProvider())
    p = FakeProvider()
    kw = {}
    if bump == "model":
        p.model = "jev-1.14.0"
    elif bump == "prompt":
        p.prompt_version = "jev-rubric-000000000000"
    else:
        kw["taxonomy_version"] = "1.1.0"
    assert run(env, p, **kw).action == "classified" and p.calls == 1
    assert run(env, p, **kw).provider_calls == 0


def test_provider_failure_keeps_stale_entry_and_retries_next_run(env):
    run(env, FakeProvider())
    good = entries(env)[VID]
    env.src.write_text(md(body="변경"), encoding="utf-8")
    failing = FakeProvider(fail=True)
    out = run(env, failing)
    assert out.action == "failed" and failing.calls == 1
    stale = entries(env)[VID]
    assert stale["error"].startswith("RuntimeError") and stale["stale"] is True and stale["cache_key"] is None
    assert stale["category_scores"] == good["category_scores"] and stale["classified_at"] == good["classified_at"]
    assert stale["content_hash"] == good["content_hash"]  # scores still describe the old bytes

    healthy = FakeProvider()
    summary = cl.retry_errors(env.index, env.root / "source", provider=healthy)
    assert summary.provider_calls == 1 and entries(env)[VID]["error"] is None
    assert cl.retry_errors(env.index, env.root / "source", provider=healthy).provider_calls == 0


def test_new_source_failure_records_error_entry_then_retries(env):
    run(env, FakeProvider(fail=True))
    assert entries(env)[VID]["error"]
    assert run(env, FakeProvider()).action == "classified"


def test_missing_key_makes_no_call_and_marks_retry(env, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    monkeypatch.setattr(cl, "_PROVIDER", None)
    out = cl.classify_if_needed(env.src, env.index)
    assert out.action == "failed" and out.provider_calls == 0 and "JEV_API_KEY_FILE" in out.error
    assert entries(env)[VID]["error"]


def test_source_markdown_and_manifest_bytes_unchanged(env):
    src, man = env.src.read_bytes(), env.manifest.read_bytes()
    run(env, FakeProvider())
    run(env, FakeProvider(fail=True), force=True)
    assert env.src.read_bytes() == src and env.manifest.read_bytes() == man


def test_identity_is_video_id_from_front_matter(env):
    assert cl.resolve_identity(md().encode())[0] == VID
    assert cl.resolve_identity(md(url=False).encode())[0] == VID
    with pytest.raises(cl.UnresolvedSource):
        cl.resolve_identity(b"---\ntitle: x\n---\nbody")
    with pytest.raises(cl.UnresolvedSource):
        cl.resolve_identity(b"---\nvid: AAAAAAAAAAA\nsource_url: https://youtu.be/BBBBBBBBBBB\n---\n")
    env.src.write_text("---\ntitle: x\n---\nbody", encoding="utf-8")
    p = FakeProvider()
    assert run(env, p).action == "unresolved" and p.calls == 0 and not env.index.exists()


def test_renamed_drive_file_keeps_identity_and_cache(env):
    p = FakeProvider()
    run(env, p)
    renamed = env.src.with_name("새이름_" + env.src.name)
    env.src.rename(renamed)
    out = cl.classify_if_needed(renamed, env.index, provider=p)
    assert out.action == "cached" and p.calls == 1
    assert entries(env)[VID]["lineage"]["drive_name"] == renamed.name


def test_existing_index_rewritten_in_place_with_prev(env):
    run(env, FakeProvider())
    inode = env.index.stat().st_ino
    first = env.index.read_bytes()
    env.src.write_text(md(body="v2"), encoding="utf-8")
    run(env, FakeProvider())
    assert env.index.stat().st_ino == inode
    assert ix.backup_path(env.index).read_bytes() == first


def test_v1_index_is_refused_until_migrated(env):
    env.index.write_text(json.dumps({"schema_version": 1, "entries": {}}), encoding="utf-8")
    with pytest.raises(ix.IndexError_, match="migrate"):
        run(env, FakeProvider())


# --- publish hook -----------------------------------------------------------------

def _publish(tmp_path, root, monkeypatch, enabled, provider):
    from scripts.drive_yt_summary.publish import publish_final_md

    monkeypatch.delenv("P03_DRIVE_SYNC_ENABLED", raising=False)
    if enabled:
        monkeypatch.setenv(cl.ENABLED_ENV, "1")
    else:
        monkeypatch.delenv(cl.ENABLED_ENV, raising=False)
    monkeypatch.setattr(cl, "_PROVIDER", provider)
    (tmp_path / "base").mkdir(exist_ok=True)
    (tmp_path / "vault").mkdir(exist_ok=True)
    return publish_final_md(rel_path=f"2026_10_06/{VID}.md", content=md(), staging_path=None,
                            md_root=str(tmp_path / "vault"), mirror=False, base_path=str(tmp_path / "base"),
                            work_path=str(tmp_path / "base"), sync_root=str(root), video_id=VID)


def test_publish_hook_off_by_default(tmp_path, monkeypatch):
    root = tmp_path / "YT"
    (root / "source").mkdir(parents=True)
    p = FakeProvider()
    res = _publish(tmp_path, root, monkeypatch, False, p)
    assert res.drive_ok and res.classify_action == "skipped" and p.calls == 0
    assert not (root / ix.INDEX_FILENAME).exists()


def test_publish_hook_classifies_once_and_failure_never_breaks_publish(tmp_path, monkeypatch):
    root = tmp_path / "YT"
    (root / "source").mkdir(parents=True)
    p = FakeProvider()
    res = _publish(tmp_path, root, monkeypatch, True, p)
    assert res.drive_ok and res.classify_action == "classified" and p.calls == 1
    res = _publish(tmp_path, root, monkeypatch, True, p)
    assert res.drive_action == "unchanged" and res.classify_action == "cached" and p.calls == 1

    (root / ix.INDEX_FILENAME).write_text("{not json", encoding="utf-8")  # corrupt index
    res = _publish(tmp_path, root, monkeypatch, True, FakeProvider())
    assert res.drive_ok and res.classify_action == "failed"


# --- migration --------------------------------------------------------------------

def _v1_entry(vid, drive_id, content_hash, at, ai=10):
    scores = {t: 0 for t in TOPIC_IDS}
    scores["ai_tech"] = ai
    return {
        "source_id": f"src-gdrive-{hashlib.sha256(drive_id.encode()).hexdigest()[:32]}",
        "producer": {"system": "google-drive", "object_id": "root"}, "drive_file_id": drive_id,
        "id_source": "xattr", "front_matter_mode": "full", "content_hash": content_hash,
        "source_url": f"https://www.youtube.com/watch?v={vid}", "category_scores": scores,
        "reason": "r", "score_semantics": "uncalibrated_relevance: x", "taxonomy_version": "1.0.0",
        "classifier": "jev", "model": "jev-1.13.0", "prompt_version": "jev-rubric-b03a101ddb34",
        "classified_at": at, "stale": False, "error": None,
        "cache_key": ix.cache_key(content_hash, "1.0.0", "jev", "jev-1.13.0", "jev-rubric-b03a101ddb34"),
        "inventory_signature": [1, 2],
        "details": {"categories": {"ai_tech": {"confidence": 0.5, "probabilities": {"0": 1.0}}}},
    }


def _v1(tmp_path):
    h1, h2 = "a" * 64, "b" * 64
    raw = [_v1_entry("AAAAAAAAAAA", "d1", h1, "2026-10-06T02:44:00Z"),
           _v1_entry("BBBBBBBBBBB", "d2", h2, "2026-10-06T02:44:00Z", ai=20),
           _v1_entry("BBBBBBBBBBB", "d3", h2, "2026-10-06T02:46:00Z", ai=30)]
    doc = {"schema_version": 1, "taxonomy_version": "1.0.0", "config_sha256": "0" * 64,
           "classifier_config": {"classifier": "jev", "model": "jev-1.13.0", "prompt_version": "jev-rubric-b03a101ddb34"},
           "entries": {e["source_id"]: e for e in raw}}
    path = tmp_path / ix.INDEX_FILENAME
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path, doc


def test_migration_preserves_fields_merges_duplicates_and_writes_in_place(tmp_path):
    path, v1 = _v1(tmp_path)
    original = path.read_bytes()
    inode = path.stat().st_ino
    report = mg.migrate_file(path)
    assert report["written"] and report["before"] == 3 and report["after"] == 2
    assert report["duplicates_merged"] == 1 and report["duplicate_video_ids"] == ["BBBBBBBBBBB"]
    assert report["parity_mismatches"] == [] and report["cache_key_mismatches"] == 0 and report["unresolvable"] == []
    assert path.stat().st_ino == inode and ix.backup_path(path).read_bytes() == original
    v2 = json.loads(path.read_text(encoding="utf-8"))
    ix.validate(v2)
    b = v2["entries"]["BBBBBBBBBBB"]
    assert b["category_scores"]["ai_tech"] == 30 and b["classified_at"] == "2026-10-06T02:46:00Z"  # newest wins
    assert b["lineage"]["drive_file_ids"] == ["d3", "d2"]
    a_old = next(e for e in v1["entries"].values() if e["drive_file_id"] == "d1")
    for k in mg.PRESERVED:
        assert v2["entries"]["AAAAAAAAAAA"][k] == a_old[k]
    assert mg.migrate_file(path)["written"] is False  # idempotent


def test_migrated_entry_is_cache_valid_zero_calls(tmp_path):
    root = tmp_path / "YT"
    (root / "source").mkdir(parents=True)
    src = root / "source" / "x.md"
    src.write_text(md(), encoding="utf-8")
    h = hashlib.sha256(src.read_bytes()).hexdigest()
    e = _v1_entry(VID, "d1", h, "2026-10-06T02:44:00Z")
    doc = {"schema_version": 1, "taxonomy_version": "1.0.0", "entries": {e["source_id"]: e},
           "classifier_config": {"classifier": "jev", "model": "jev-1.13.0", "prompt_version": "jev-rubric-b03a101ddb34"}}
    index = root / ix.INDEX_FILENAME
    index.write_text(json.dumps(doc), encoding="utf-8")
    mg.migrate_file(index)
    p = FakeProvider()  # same provider/model/prompt as the live index
    assert cl.classify_if_needed(src, index, provider=p).action == "cached" and p.calls == 0


def test_migration_dry_run_does_not_write(tmp_path):
    path, _ = _v1(tmp_path)
    before = path.read_bytes()
    report = mg.migrate_file(path, dry_run=True)
    assert report["written"] is False and report["after"] == 2 and path.read_bytes() == before


def test_cli_backfill_dry_run_and_paths(env, monkeypatch, capsys):
    monkeypatch.setattr(cl, "_PROVIDER", FakeProvider())
    assert cli_main(["--sync-root", str(env.root), "classify", "--path", str(env.src), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["would_call"] == 1
    assert cli_main(["--sync-root", str(env.root), "classify", "--path", str(env.src)]) == 0
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 1
    assert cli_main(["--sync-root", str(env.root), "classify", "--path", str(env.src)]) == 0
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 0
    assert cli_main(["--sync-root", str(env.root), "classify", "--path", str(env.src), "--force"]) == 0
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 1
