"""SUE-1298 follow-up: iCloud/Obsidian measure the 255-byte cap on the NFD form.

The two real names from the 2026-10-07 03:00 run are 134 / 160 UTF-8 bytes in NFC but
263 / 289 in NFD, so the NFC-only budget of PR #9 left them untouched and storage cut
them to 255 bytes (losing the ``luna-low`` suffix, and for one the video ID).
"""

from __future__ import annotations

import sys
import unicodedata as ud
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from filename_utils import MAX_FILENAME_BYTES, fit_filename  # noqa: E402
from scripts.drive_yt_summary.sync import find_canonical_for_truncated, run_sync  # noqa: E402
from scripts.drive_yt_summary.state import SyncState, SyncStateEntry, save_state  # noqa: E402

JYY = "교양의 시대_작은 섬나라 영국은 어떻게 불패의 함대를 만들어냈을까_JyyAGZ1-r94_ko-orig_auto_subs_luna-low.md"
BE5B = (
    "실밸개발자_코드 리뷰가 병목이 되는 이유 _ 메타 시니어 엔지니어가 "
    "AI 코드 리뷰 하는법_Be5b6D1IoLI_ko-orig_auto_subs_luna-low.md"
)
CASES = [
    (JYY, "JyyAGZ1-r94", "_JyyAGZ1-r94_ko-orig_auto_subs_luna-low.md", "교양의 시대_"),
    (BE5B, "Be5b6D1IoLI", "_Be5b6D1IoLI_ko-orig_auto_subs_luna-low.md", "실밸개발자_"),
]


def _b(s: str, form: str) -> int:
    return len(ud.normalize(form, s).encode("utf-8"))


def test_fixture_names_match_the_run() -> None:
    assert (_b(JYY, "NFC"), _b(JYY, "NFD")) == (134, 263)
    assert (_b(BE5B, "NFC"), _b(BE5B, "NFD")) == (160, 289)


@pytest.mark.parametrize("name,vid,tail,prefix", CASES)
def test_nfd_over_budget_is_shortened_and_keeps_id_and_suffix(name, vid, tail, prefix) -> None:
    out = fit_filename(name, vid, protect_prefix=prefix)
    assert out != name
    assert _b(out, "NFD") <= MAX_FILENAME_BYTES
    assert _b(out, "NFC") <= MAX_FILENAME_BYTES
    assert out.endswith(tail)
    assert out.startswith(prefix)
    assert out == ud.normalize("NFC", out)  # written normalization stays NFC


@pytest.mark.parametrize("name,vid,tail,prefix", CASES)
def test_idempotent(name, vid, tail, prefix) -> None:
    once = fit_filename(name, vid, protect_prefix=prefix)
    assert fit_filename(once, vid, protect_prefix=prefix) == once


def test_short_nfd_name_is_unchanged() -> None:
    name = "채널_짧은 제목_dQw4w9WgXcQ_ko_subs_luna-low.md"
    assert fit_filename(name, "dQw4w9WgXcQ") == name


# --- sync: a vault copy truncated by storage must not become a second Drive file ---


def _truncate_nfd(name: str, limit: int = 255) -> str:
    """Storage-style cut: shrink the stem so the NFD name is <= limit, keep ``.md``."""
    stem = ud.normalize("NFD", name)[:-3]
    out = ""
    for ch in stem:
        if len((out + ch + ".md").encode("utf-8")) > limit:
            break
        out += ch
    return out + ".md"


def _md() -> str:
    return "---\nformat_version: 4.1\ntitle: T\n---\n\nBody\n"


def _setup(tmp_path: Path, name: str):
    vault = tmp_path / "vault"
    day = vault / "2026_10_07"
    day.mkdir(parents=True)
    drive = tmp_path / "YT_summary"
    (drive / "source").mkdir(parents=True)
    (drive / "legacy").mkdir()
    base, work = tmp_path / "base", tmp_path / "work"
    base.mkdir()
    work.mkdir()
    trunc = _truncate_nfd(name)
    assert trunc != ud.normalize("NFD", name) and _b(trunc, "NFD") >= 250
    (day / trunc).write_text(_md(), encoding="utf-8")
    return vault, drive, base, work, trunc


def _sync(vault, drive, base, work):
    return run_sync(
        dry_run=False,
        migrate_legacy=False,
        md_path=str(vault),
        sync_root=str(drive),
        base_path=str(base),
        work_path=str(work),
    )


@pytest.mark.parametrize("name", [JYY, BE5B])
def test_sync_skips_truncated_copy_when_canonical_in_state(tmp_path: Path, name: str) -> None:
    vault, drive, base, work, _ = _setup(tmp_path, name)
    canon = drive / "source" / name
    canon.write_text(_md(), encoding="utf-8")
    # No state entry: the source/ listing alone must be enough.
    result = _sync(vault, drive, base, work)
    assert result.created == 0 and result.errors == 0
    assert [p.name for p in (drive / "source").iterdir()] == [name]


@pytest.mark.parametrize("name", [JYY, BE5B])
def test_sync_skips_truncated_copy_via_state_entry(tmp_path: Path, name: str) -> None:
    vault, drive, base, work, _ = _setup(tmp_path, name)
    rel = f"2026_10_07/{name}"
    from scripts.drive_yt_summary.config import load_config

    cfg = load_config(str(base), str(work), str(vault), sync_root=str(drive))
    save_state(
        cfg.state_path,
        SyncState(
            files={
                rel: SyncStateEntry(
                    relative_path=rel,
                    content_hash="x",
                    dest_path=str(drive / "source" / name),
                    drive_name=name,
                    updated_at="2026-10-07T00:00:00+00:00",
                )
            }
        ),
    )
    result = _sync(vault, drive, base, work)
    assert result.created == 0 and result.errors == 0
    assert list((drive / "source").iterdir()) == []  # nothing minted from the truncated name


def test_sync_still_creates_unrelated_note(tmp_path: Path) -> None:
    vault, drive, base, work, _ = _setup(tmp_path, JYY)
    (drive / "source" / JYY).write_text(_md(), encoding="utf-8")
    other = vault / "2026_10_07" / "다른채널_다른 제목_AAAAAAAAAAA_ko_subs_luna-low.md"
    other.write_text(_md(), encoding="utf-8")
    result = _sync(vault, drive, base, work)
    assert result.created == 1
    assert (drive / "source" / other.name).is_file()


def test_find_canonical_requires_strict_prefix_near_cap() -> None:
    trunc = _truncate_nfd(JYY)
    assert find_canonical_for_truncated(f"d/{trunc}", [], [JYY]) == JYY
    assert find_canonical_for_truncated(f"d/{trunc}", [], []) is None
    assert find_canonical_for_truncated("d/짧은_이름.md", [], ["짧은_이름_더긴.md"]) is None
