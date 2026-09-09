#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the bounded P03 -> Learning Intelligence source adapter (SUE-733)."""

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.intelligence_source_adapter import (  # noqa: E402
    FORMAT_VERSION,
    adapter_enabled,
    load_state,
    prune_ledger,
    resolve_window_start,
    run_adapter,
    select_rows,
    to_record,
    write_state,
)

TODAY = "2026-09-09"


def _row(vid, date, **kw):
    row = {
        "schema_version": 1,
        "vid": vid,
        "transcript_date": date,
        "upload_date": kw.get("upload_date", date),
        "channel": kw.get("channel", "채널"),
        "title": kw.get("title", f"title-{vid}"),
        "tldr": kw.get("tldr", f"tldr for {vid}"),
        "tags": kw.get("tags", ["ai"]),
        "source_url": kw.get("source_url", f"https://www.youtube.com/watch?v={vid}"),
        "md_path_rel": kw.get("md_path_rel", f"{date.replace('-', '_')}/{vid}.md"),
        "lang": kw.get("lang", "ko"),
        "suffix": kw.get("suffix", "5-mini"),
        "source": kw.get("source", "live_pipeline"),
    }
    row.update({k: v for k, v in kw.items() if k not in row})
    return row


def _catalog(tmp, rows):
    path = os.path.join(tmp, "note_catalog.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


# --------------------------------------------------------------------------
# bounded read
# --------------------------------------------------------------------------


def test_window_excludes_older_rows():
    rows = [_row("aaaaaaaaaaa", "2026-09-09"), _row("bbbbbbbbbbb", "2026-01-02")]
    selected = select_rows(rows, window_start="2026-09-07")
    assert [r["vid"] for r in selected] == ["aaaaaaaaaaa"]


def test_rows_without_durable_identity_or_url_are_dropped():
    rows = [
        _row("aaaaaaaaaaa", TODAY),
        _row("", TODAY),
        _row("ccccccccccc", TODAY, source_url=""),
    ]
    assert [r["vid"] for r in select_rows(rows, "2026-09-01")] == ["aaaaaaaaaaa"]


def test_duplicate_video_id_collapses_to_one_record():
    """Same video, two language/summariser rows -> one discovery record."""
    rows = [
        _row("aaaaaaaaaaa", "2026-09-08", lang="ko", tldr=""),
        _row("aaaaaaaaaaa", "2026-09-09", lang="en-orig", tldr="real summary"),
    ]
    selected = select_rows(rows, "2026-09-01")
    assert len(selected) == 1
    assert selected[0]["lang"] == "en-orig"


def test_window_start_prefers_checkpoint_with_recovery_slack():
    state = {"last_transcript_date": "2026-09-08"}
    assert resolve_window_start(state, None, None, 2, TODAY) == "2026-09-06"


def test_explicit_since_days_overrides_checkpoint():
    state = {"last_transcript_date": "2026-09-08"}
    assert resolve_window_start(state, 3, None, 2, TODAY) == "2026-09-06"
    assert resolve_window_start(state, 30, None, 2, TODAY) == "2026-08-10"


# --------------------------------------------------------------------------
# contract / provenance
# --------------------------------------------------------------------------


def test_record_carries_required_contract_fields():
    rec = to_record(_row("aaaaaaaaaaa", TODAY))
    for field in (
        "format_version",
        "source_type",
        "video_id",
        "source_url",
        "channel",
        "title",
        "upload_date",
        "processed_at",
        "summary_ref",
        "tldr",
        "tags",
    ):
        assert field in rec, field
    assert rec["source_type"] == "youtube"
    assert rec["format_version"] == FORMAT_VERSION
    assert rec["source_url"].endswith("aaaaaaaaaaa")


def test_record_marks_summary_as_derived_not_primary_evidence():
    rec = to_record(_row("aaaaaaaaaaa", TODAY))
    assert rec["evidence_role"] == "derived-summary"


def test_provenance_traces_back_to_catalog_row():
    rec = to_record(_row("aaaaaaaaaaa", TODAY, source="live_pipeline"))
    prov = rec["provenance"]
    assert prov["system"] == "p03"
    assert prov["catalog"] == "index/note_catalog.jsonl"
    assert prov["catalog_source"] == "live_pipeline"
    assert rec["summary_ref"].endswith(".md")


def test_summary_hash_is_opt_in():
    assert "summary_hash" not in to_record(_row("aaaaaaaaaaa", TODAY))
    assert "summary_hash" in to_record(_row("aaaaaaaaaaa", TODAY), with_summary_hash=True)


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


def test_second_run_over_unchanged_catalog_emits_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        cat = _catalog(tmp, [_row("aaaaaaaaaaa", TODAY), _row("bbbbbbbbbbb", TODAY)])
        state_file = os.path.join(tmp, "state.json")

        first = run_adapter(cat, state_file, since_days=7, today=TODAY)
        assert first["stats"]["emitted"] == 2
        write_state(state_file, first["state"])

        second = run_adapter(cat, state_file, since_days=7, today=TODAY)
        assert second["stats"]["emitted"] == 0
        assert second["stats"]["skipped_already_emitted"] == 2
        assert second["stats"]["next_checkpoint"] == first["stats"]["next_checkpoint"]


def test_only_new_material_is_emitted_on_the_next_run():
    with tempfile.TemporaryDirectory() as tmp:
        cat = _catalog(tmp, [_row("aaaaaaaaaaa", "2026-09-08")])
        state_file = os.path.join(tmp, "state.json")
        first = run_adapter(cat, state_file, since_days=7, today=TODAY)
        write_state(state_file, first["state"])

        cat = _catalog(tmp, [_row("aaaaaaaaaaa", "2026-09-08"), _row("bbbbbbbbbbb", TODAY)])
        second = run_adapter(cat, state_file, today=TODAY)
        assert [r["video_id"] for r in second["records"]] == ["bbbbbbbbbbb"]


def test_replay_re_emits_without_advancing_the_checkpoint():
    with tempfile.TemporaryDirectory() as tmp:
        cat = _catalog(tmp, [_row("aaaaaaaaaaa", TODAY)])
        state_file = os.path.join(tmp, "state.json")
        first = run_adapter(cat, state_file, since_days=7, today=TODAY)
        write_state(state_file, first["state"])

        replay = run_adapter(cat, state_file, since_days=7, replay=True, today=TODAY)
        assert replay["stats"]["emitted"] == 1
        assert replay["state_changed"] is False
        assert load_state(state_file)["last_transcript_date"] == first["state"]["last_transcript_date"]


def test_ledger_stays_bounded():
    emitted = {"old": "2026-01-01", "new": "2026-09-09"}
    pruned = prune_ledger(emitted, "2026-09-09", retention_days=30)
    assert pruned == {"new": "2026-09-09"}


def test_missing_catalog_is_fail_soft():
    with tempfile.TemporaryDirectory() as tmp:
        result = run_adapter(
            os.path.join(tmp, "absent.jsonl"), os.path.join(tmp, "state.json"), today=TODAY
        )
        assert result["records"] == []
        assert result["stats"]["catalog_rows_in_window"] == 0


def test_corrupt_state_file_restarts_clean_instead_of_unbounding():
    with tempfile.TemporaryDirectory() as tmp:
        state_file = os.path.join(tmp, "state.json")
        with open(state_file, "w", encoding="utf-8") as f:
            f.write("{not json")
        assert load_state(state_file)["last_transcript_date"] is None


# --------------------------------------------------------------------------
# disable switch
# --------------------------------------------------------------------------


def test_adapter_can_be_disabled_without_touching_the_p03_runtime():
    assert adapter_enabled({}) is True
    assert adapter_enabled({"P03_INTELLIGENCE_ADAPTER": "on"}) is True
    for off in ("off", "0", "false", "disabled", "no"):
        assert adapter_enabled({"P03_INTELLIGENCE_ADAPTER": off}) is False, off


def test_adapter_module_is_not_imported_by_the_canonical_pipeline():
    """The handoff is an optional script, never a pipeline dependency."""
    for entry in ("main.py", "stt_function_v3.py", "pipeline_context.py"):
        text = (PROJECT_ROOT / entry).read_text(encoding="utf-8")
        assert "intelligence_source_adapter" not in text, entry


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"ok ({len(fns)} tests)")
