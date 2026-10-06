"""255-byte note filenames keep the video ID; direct input_df URLs mirror to Obsidian."""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from filename_utils import (
    ELLIPSIS,
    FILENAME_BUDGET_BYTES,
    fit_filename,
    truncate_to_bytes,
)
from scripts.drive_yt_summary.fs_transport import short_tmp_path

VID = "dQw4w9WgXcQ"
TAIL = f"_{VID}_ko_subs_5-mini.md"
PREFIX = "채널_"


def _blen(s):
    """max(NFC, NFD) UTF-8 bytes: iCloud/Obsidian measure the decomposed form."""
    return max(
        len(unicodedata.normalize("NFC", s).encode("utf-8")),
        len(unicodedata.normalize("NFD", s).encode("utf-8")),
    )


def _check(out, tail=TAIL):
    assert _blen(out) <= FILENAME_BUDGET_BYTES
    assert out.endswith(tail)
    assert VID in out
    out.encode("utf-8").decode("utf-8")  # valid UTF-8, no half characters


TITLES = {
    "korean": "한글제목" * 80,
    "emoji": "🔥🚀😀" * 90,
    "mixed": "Fed 금리 인하 🚀 전망 ABC " * 30,
    "zwj_emoji": "👨‍👩‍👧‍👦" * 40,
}


@pytest.mark.parametrize("kind", TITLES)
def test_long_titles_fit_and_keep_id(kind):
    name = f"{PREFIX}{TITLES[kind]}{TAIL}"
    assert _blen(name) > FILENAME_BUDGET_BYTES
    out = fit_filename(name, VID, protect_prefix=PREFIX)
    _check(out)
    assert out.startswith(PREFIX)
    assert ELLIPSIS in out  # room existed, so the ellipsis is used


def test_exactly_255_bytes_unchanged():
    base = f"{PREFIX}{{t}}{TAIL}"
    room = FILENAME_BUDGET_BYTES - _blen(base.format(t=""))
    title = "a" * room  # ASCII: identical in NFC and NFD
    name = base.format(t=title)
    assert _blen(name) == FILENAME_BUDGET_BYTES
    assert fit_filename(name, VID, protect_prefix=PREFIX) == name


def test_256_bytes_shrinks_by_title_only():
    base = f"{PREFIX}{{t}}{TAIL}"
    room = FILENAME_BUDGET_BYTES + 1 - _blen(base.format(t=""))
    name = base.format(t="a" * room)
    assert _blen(name) == FILENAME_BUDGET_BYTES + 1
    out = fit_filename(name, VID, protect_prefix=PREFIX)
    _check(out)
    assert _blen(out) == FILENAME_BUDGET_BYTES  # 1 byte over -> only trimmed, ellipsis fills
    assert out.startswith(PREFIX)


def test_short_name_untouched():
    name = f"{PREFIX}짧은제목{TAIL}"
    assert fit_filename(name, VID, protect_prefix=PREFIX) == name


def test_idempotent_no_stacked_ellipsis():
    name = f"{PREFIX}{TITLES['korean']}{TAIL}"
    once = fit_filename(name, VID, protect_prefix=PREFIX)
    twice = fit_filename(once, VID, protect_prefix=PREFIX)
    assert once == twice
    assert twice.count(ELLIPSIS) == 1


def test_second_stage_derived_name_refits():
    # txt name already fitted, then channel prefix + suffix added at the md stage.
    txt = fit_filename(f"{TITLES['mixed']}_{VID}_ko_subs.txt", VID)
    md = f"{PREFIX}{txt[:-4]}_5-mini.md"
    out = fit_filename(md, VID, protect_prefix=PREFIX)
    _check(out)
    assert out.count(ELLIPSIS) == 1


def test_huge_prefix_squeezed_but_id_kept():
    prefix = "채" * 200 + "_"
    out = fit_filename(f"{prefix}제목{TAIL}", VID, protect_prefix=prefix)
    _check(out)


def test_truncate_never_splits_multibyte():
    for n in range(40):
        s = truncate_to_bytes("한🚀글é끝" * 5, n)
        assert _blen(s) <= n
        s.encode("utf-8")


def test_ellipsis_dropped_when_it_does_not_fit():
    assert truncate_to_bytes("가나다", 2) == ""
    assert truncate_to_bytes("abcdef", 2, ellipsis="…") == "ab"


def test_tmp_name_short_for_255_byte_name(tmp_path):
    name = "가" * 84 + "abc"  # 255 bytes
    assert len(name.encode("utf-8")) == 255
    tmp = short_tmp_path(tmp_path / name)
    assert _blen(tmp.name) < 64
    assert tmp.parent == tmp_path


def test_255_byte_file_written_end_to_end(tmp_path):
    name = fit_filename(f"{PREFIX}{TITLES['korean']}{TAIL}", VID, protect_prefix=PREFIX)
    p = tmp_path / "2026_10_07" / name
    main.atomic_write_text_with_retry(str(p), "본문", encoding="utf-8-sig")
    assert p.read_text(encoding="utf-8-sig") == "본문"
    assert [x.name for x in p.parent.iterdir()] == [name]


# ---- obsidian mirror policy: decided by input source, not channel lookup ----

DIRECT = "https://www.youtube.com/watch?v=direct1"
CH_REC = "https://www.youtube.com/watch?v=chrec"
CH_OFF = "https://www.youtube.com/watch?v=choff"
CH_UNKNOWN = "https://www.youtube.com/watch?v=unknown"
MARKERS = {CH_REC: True, CH_OFF: False}  # CH_UNKNOWN has no channel_df entry


def test_direct_url_mirrors():
    assert main.resolve_obsidian_mirror(DIRECT, {DIRECT}, MARKERS) is True


def test_direct_url_mirrors_even_if_also_in_non_recording_channel():
    assert main.resolve_obsidian_mirror(CH_OFF, {CH_OFF}, MARKERS) is True


def test_recording_channel_mirrors():
    assert main.resolve_obsidian_mirror(CH_REC, {DIRECT}, MARKERS) is True


def test_non_recording_channel_does_not_mirror():
    assert main.resolve_obsidian_mirror(CH_OFF, {DIRECT}, MARKERS) is False


def test_unknown_channel_crawl_video_does_not_mirror():
    assert main.resolve_obsidian_mirror(CH_UNKNOWN, {DIRECT}, MARKERS) is False
    assert main.resolve_obsidian_mirror(CH_UNKNOWN, set(), {}) is False


def test_concise_txt_write_path_refits_after_suffix(tmp_path):
    # A txt name already at the 255-byte cap, then the LLM suffix is added (concise write).
    txt = fit_filename(f"{TITLES['korean']}_{VID}_ko_subs.txt", VID)
    assert _blen(txt) <= FILENAME_BUDGET_BYTES
    out = main.derive_output_name(txt, "5-mini", VID)
    assert _blen(out) <= FILENAME_BUDGET_BYTES and out.endswith("_ko_subs_5-mini.txt") and VID in out
    p = tmp_path / out
    main.atomic_write_text_with_retry(str(p), "요약", encoding="utf-8-sig")
    assert p.read_text(encoding="utf-8-sig") == "요약"
    md = main.derive_output_name(txt, "5-mini", VID, "md")
    assert _blen(md) <= FILENAME_BUDGET_BYTES and md.endswith("_5-mini.md") and VID in md


def test_derive_exact_cap_txt_plus_suffix():
    base = f"{{t}}_{VID}_ko_subs.txt"
    room = FILENAME_BUDGET_BYTES - _blen(base.format(t=""))
    txt = base.format(t="a" * room)
    assert _blen(txt) == FILENAME_BUDGET_BYTES
    out = main.derive_output_name(txt, "5-mini", VID)
    assert _blen(out) <= FILENAME_BUDGET_BYTES and VID in out
