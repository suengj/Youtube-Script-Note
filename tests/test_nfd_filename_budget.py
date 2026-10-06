"""SUE-1298 follow-up: iCloud/Obsidian measure the 255-byte cap on the NFD form.

The two real names from the 2026-10-07 03:00 run are 134 / 160 UTF-8 bytes in NFC but
263 / 289 in NFD, so the NFC-only budget of PR #9 left them untouched and storage cut
them to 255 bytes (losing the ``luna-low`` suffix, and for one the video ID).
"""

from __future__ import annotations

import hashlib
import sys
import unicodedata as ud
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from filename_utils import FILENAME_BUDGET_BYTES, MAX_FILENAME_BYTES, fit_filename, parse_note_name  # noqa: E402
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
    assert _b(out, "NFD") <= FILENAME_BUDGET_BYTES
    assert _b(out, "NFC") <= FILENAME_BUDGET_BYTES
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
def test_sync_skips_truncated_copy_of_known_canonical(tmp_path: Path, name: str) -> None:
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
                    content_hash=H,
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
    _seed_state(vault, drive, base, work, [f"2026_10_07/{JYY}"])
    other = vault / "2026_10_07" / "다른채널_다른 제목_AAAAAAAAAAA_ko_subs_luna-low.md"
    other.write_text(_md(), encoding="utf-8")
    result = _sync(vault, drive, base, work)
    assert result.created == 1
    assert (drive / "source" / other.name).is_file()


H = hashlib.sha256(_md().encode("utf-8")).hexdigest()


def _entries(rels, h=H, dest_dir=None):
    return {
        r: SyncStateEntry(
            relative_path=r,
            content_hash=h,
            dest_path=str((dest_dir or Path("/nonexistent")) / r.rpartition("/")[2]),
            drive_name=r.rpartition("/")[2],
            updated_at="2026-10-07T00:00:00+00:00",
        )
        for r in rels
    }


def _seed_state(vault, drive, base, work, rels, h=H):
    from scripts.drive_yt_summary.config import load_config

    cfg = load_config(str(base), str(work), str(vault), sync_root=str(drive))
    save_state(
        cfg.state_path,
        SyncState(
            files={
                r: SyncStateEntry(
                    relative_path=r,
                    content_hash=h,
                    dest_path=str(drive / "source" / r.rpartition("/")[2]),
                    drive_name=r.rpartition("/")[2],
                    updated_at="2026-10-07T00:00:00+00:00",
                )
                for r in rels
            }
        ),
    )


def test_prefix_of_longer_name_below_cap_window_is_created(tmp_path: Path) -> None:
    """A*240 + .md is a prefix of A*240 + _X.md but is a genuine note, not a truncation."""
    vault, drive, base, work, trunc = _setup(tmp_path, JYY)
    (vault / "2026_10_07" / trunc).unlink()
    short = "A" * 240 + ".md"
    longer = "A" * 240 + "_" + "X" * 30 + ".md"
    (vault / "2026_10_07" / short).write_text(_md(), encoding="utf-8")
    _seed_state(vault, drive, base, work, [f"2026_10_07/{longer}"])
    result = _sync(vault, drive, base, work)
    assert result.created == 1
    assert (drive / "source" / short).is_file()


def test_truncated_candidate_with_canonical_under_cap_is_created(tmp_path: Path) -> None:
    vault, drive, base, work, trunc = _setup(tmp_path, JYY)
    cand = _truncate_nfd(JYY, 253)
    assert _b(cand, "NFD") == 253
    canonical = cand[:-3] + "zz.md"  # 2 bytes longer: 255, within the cap -> not a truncation
    assert _b(canonical, "NFD") == 255
    assert find_canonical_for_truncated(f"2026_10_07/{cand}", H, _entries([f"2026_10_07/{canonical}"])) is None
    (vault / "2026_10_07" / trunc).unlink()
    (vault / "2026_10_07" / cand).write_text(_md(), encoding="utf-8")
    _seed_state(vault, drive, base, work, [f"2026_10_07/{canonical}"])
    assert _sync(vault, drive, base, work).created == 1


def test_cross_folder_canonical_is_not_a_match(tmp_path: Path) -> None:
    vault, drive, base, work, trunc = _setup(tmp_path, JYY)
    _seed_state(vault, drive, base, work, [f"2026_10_06/{JYY}"])
    result = _sync(vault, drive, base, work)
    assert result.created == 1
    assert find_canonical_for_truncated(f"2026_10_07/{trunc}", H, _entries([f"2026_10_06/{JYY}"])) is None


def test_source_listing_without_folder_is_not_enough(tmp_path: Path) -> None:
    vault, drive, base, work, trunc = _setup(tmp_path, JYY)
    (drive / "source" / JYY).write_text(_md(), encoding="utf-8")
    result = _sync(vault, drive, base, work)
    assert result.created == 1  # folder unknown -> do not skip


@pytest.mark.parametrize("name", [JYY, BE5B])
def test_find_canonical_matches_real_names(name: str) -> None:
    trunc = _truncate_nfd(name)
    assert 253 <= _b(trunc, "NFD") <= 255
    assert find_canonical_for_truncated(f"d/{trunc}", H, _entries([f"d/{name}"])) == f"d/{name}"
    assert find_canonical_for_truncated(f"d/{trunc}", H, _entries([f"d/{trunc}"])) is None  # not strict


def test_same_names_but_different_content_is_created(tmp_path: Path) -> None:
    """A genuine A*252 note next to a state entry A*252_B*12 must not be skipped."""
    vault, drive, base, work, trunc = _setup(tmp_path, JYY)
    (vault / "2026_10_07" / trunc).unlink()
    cand = "A" * 252 + ".md"
    canon = "A" * 252 + "_" + "B" * 12 + ".md"
    assert _b(cand, "NFD") == 255 and _b(canon, "NFD") > 255
    (vault / "2026_10_07" / cand).write_text(_md(), encoding="utf-8")
    # canonical has DIFFERENT recorded content
    _seed_state(vault, drive, base, work, [f"2026_10_07/{canon}"], h="0" * 64)
    assert find_canonical_for_truncated(
        f"2026_10_07/{cand}", H, _entries([f"2026_10_07/{canon}"], h="0" * 64)
    ) is None
    assert _sync(vault, drive, base, work).created == 1
    assert (drive / "source" / cand).is_file()


def test_unknown_hash_reads_drive_file_or_does_not_skip(tmp_path: Path) -> None:
    rel = f"2026_10_07/{JYY}"
    trunc = f"2026_10_07/{_truncate_nfd(JYY)}"
    f = tmp_path / JYY
    # no recorded hash and unreadable file -> no skip
    assert find_canonical_for_truncated(trunc, H, _entries([rel], h="", dest_dir=tmp_path)) is None
    # no recorded hash, readable file with equal content -> skip
    f.write_text(_md(), encoding="utf-8")
    assert find_canonical_for_truncated(trunc, H, _entries([rel], h="", dest_dir=tmp_path)) == rel
    # readable but different content -> no skip
    f.write_text(_md() + "x", encoding="utf-8")
    assert find_canonical_for_truncated(trunc, H, _entries([rel], h="", dest_dir=tmp_path)) is None


def test_a252_identical_content_not_skipped_canonical_not_p03_name(tmp_path: Path) -> None:
    cand = "A" * 252 + ".md"
    canon = "A" * 252 + "_" + "B" * 12 + ".md"
    assert _b(cand, "NFD") == 255 and _b(canon, "NFD") > 255
    assert find_canonical_for_truncated(f"d/{cand}", H, _entries([f"d/{canon}"])) is None
    vault, drive, base, work, trunc = _setup(tmp_path, JYY)
    (vault / "2026_10_07" / trunc).unlink()
    (vault / "2026_10_07" / cand).write_text(_md(), encoding="utf-8")
    _seed_state(vault, drive, base, work, [f"2026_10_07/{canon}"])  # identical content hash
    assert _sync(vault, drive, base, work).created == 1


def test_parse_note_name() -> None:
    p = parse_note_name(JYY)
    assert (p.video_id, p.lang, p.subs_type, p.llm_suffix) == ("JyyAGZ1-r94", "ko-orig", "auto_subs", "luna-low")
    assert parse_note_name("c_t_dQw4w9WgXcQ_ko_subs_5-mini.md").llm_suffix == "5-mini"
    assert parse_note_name(_truncate_nfd(JYY)) is None  # suffix cut
    assert parse_note_name(_truncate_nfd(BE5B)) is None  # ID cut


def test_two_videos_same_long_title_prefix_neither_skipped(tmp_path: Path) -> None:
    title = "공통제목" * 20
    n1 = f"채널_{title}_AAAAAAAAAAA_ko_subs_luna-low.md"
    n2 = f"채널_{title}_BBBBBBBBBBB_ko_subs_luna-low.md"
    # storage-style cut of each (ID gone) is ambiguous and well-formed names are never cut
    c1, c2 = _truncate_nfd(n1), _truncate_nfd(n2)
    ents = _entries([f"d/{n1}", f"d/{n2}"])
    for c in (c1, c2):
        assert find_canonical_for_truncated(f"d/{c}", H, ents) is None  # ambiguous: two canonicals
    # well-formed names with the same prefix, each next to the other's entry
    w1 = f"채널_{'a' * 207}_AAAAAAAAAAA_ko_subs_luna-low.md"
    w2 = f"채널_{'a' * 207}_BBBBBBBBBBB_ko_subs_luna-low.md"
    assert _b(w1, "NFD") == 255
    for w, other in ((w1, w2), (w2, w1)):
        assert find_canonical_for_truncated(f"d/{w}", H, _entries([f"d/{other}"])) is None


def test_well_formed_name_never_skipped() -> None:
    w = f"채널_{'a' * 207}_dQw4w9WgXcQ_ko_subs_luna-low.md"
    assert 253 <= _b(w, "NFD") <= 255
    longer = f"채널_{'a' * 207}_dQw4w9WgXcQ_ko_subs_luna-low-extra-long-suffix-x.md"
    assert find_canonical_for_truncated(f"d/{w}", H, _entries([f"d/{longer}"])) is None


def test_parseable_cut_suffix_is_treated_as_well_formed_and_not_skipped() -> None:
    """Owner rule: a complete ID + lang + suffix token is never skipped, even if it is a
    prefix of a longer canonical (``..._lu.md`` vs ``..._luna-low.md``)."""
    head = "t" * 228 + "_dQw4w9WgXcQ_ko_subs_"
    cut, full = head + "lu.md", head + "luna-low.md"
    assert _b(cut, "NFD") == 254 and _b(full, "NFD") > 255
    assert parse_note_name(cut).llm_suffix == "lu"
    assert find_canonical_for_truncated(f"d/{cut}", H, _entries([f"d/{full}"])) is None


def test_different_id_segment_anywhere_blocks_skip() -> None:
    head = "t" * 220 + "_dQw4w9WgXcQ_ko-orig_auto_subs_"
    canon = head + "luna-low.md"
    # candidate cut inside the suffix, but with another ID-shaped segment + lang marker
    other = "t" * 220 + "_zzzzzzzzzzz_ko-orig_auto_subs_"
    assert 253 <= _b(other + ".md", "NFD") <= 255 and _b(canon, "NFD") > 255
    # control: the same cut with the canonical ID IS a truncation
    assert find_canonical_for_truncated(f"d/{head}.md", H, _entries([f"d/{canon}"])) == f"d/{canon}"
    assert find_canonical_for_truncated(f"d/{other}.md", H, _entries([f"d/{canon}"])) is None


def test_whisper_path_names_parse() -> None:
    # Real vault names: ``{title}.m4a+vid-{ID}_{llm}.md`` (no lang / subs segment)
    p = parse_note_name("금융의 미래_ '디파이'(DeFi)를 꼭 알아야하는 이유!.m4a+vid-vRUn0DB_4PU_o1-mini.md")
    assert (p.video_id, p.lang, p.subs_type, p.llm_suffix) == ("vRUn0DB_4PU", "", "", "o1-mini")
    p = parse_note_name("암호화폐의 단점_ 개발자가 설명해드림!.m4a+vid-ML6-Ncr_TvY_o1-mini.md")
    assert (p.video_id, p.llm_suffix) == ("ML6-Ncr_TvY", "o1-mini")
    p = parse_note_name("매경월가_제목_VT42abcdefg_5-mini.md")  # ID then suffix only
    assert p.video_id == "VT42abcdefg" and p.llm_suffix == "5-mini" and p.lang == ""
    assert parse_note_name("채널_제목_dQw4w9WgXcQ_ko_luna-low.md").lang == "ko"


def test_whisper_candidate_is_well_formed_not_skipped() -> None:
    name = "채널_" + "가" * 20 + ".m4a+vid-vRUn0DB_4PU_o1-mini.md"
    assert find_canonical_for_truncated(f"d/{name}", H, _entries([f"d/{name}x"])) is None


def test_llm_suffix_validation_and_tail_overflow() -> None:
    from filename_utils import validate_llm_suffix

    for ok in ("5-mini", "dS4f", "luna-low", "luna-medium"):
        assert validate_llm_suffix(ok) == ok
    for bad in ("", "x" * 33, "a_b", "-x", "한글"):
        with pytest.raises(ValueError):
            validate_llm_suffix(bad)
    vid = "dQw4w9WgXcQ"
    huge = f"채널_제목_{vid}_ko_subs_" + "s" * 300 + ".md"
    with pytest.raises(ValueError):
        fit_filename(huge, vid, protect_prefix="채널_")


def test_budget_constants() -> None:
    assert FILENAME_BUDGET_BYTES == 240 and MAX_FILENAME_BYTES == 255


def test_extreme_long_suffix_and_500_byte_korean_title() -> None:
    vid = "dQw4w9WgXcQ"
    tail = f"_{vid}_en-US-orig_auto_subs_luna-medium.md"
    title = "한" * 170  # 510 bytes NFC, 1190 NFD
    name = f"채널_{title}{tail}"
    out = fit_filename(name, vid, protect_prefix="채널_")
    assert _b(out, "NFD") <= FILENAME_BUDGET_BYTES
    assert out.endswith(tail) and out.startswith("채널_")
    assert fit_filename(out, vid, protect_prefix="채널_") == out


def test_headroom_survives_conflict_and_tmp_suffixes() -> None:
    out = fit_filename(JYY, "JyyAGZ1-r94", protect_prefix="교양의 시대_")
    for extra in (" 2", " (1)", ".tmp"):
        assert _b(out + extra, "NFD") <= MAX_FILENAME_BYTES


def test_sync_skip_window_tied_to_hard_cap() -> None:
    from scripts.drive_yt_summary import sync

    assert sync._TRUNC_MAX_BYTES == MAX_FILENAME_BYTES == 255
    assert sync._TRUNC_MIN_BYTES == 253
