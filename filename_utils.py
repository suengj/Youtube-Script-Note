"""Byte-bounded note filenames that never lose the video ID.

Most filesystems (APFS, ext4, Drive Desktop sync) cap a single name at 255 *bytes*
of UTF-8, not 255 characters; iCloud/Obsidian measures the decomposed NFD form, so the
budget here is max(NFC bytes, NFD bytes). A long Korean or emoji title therefore overflows
well before it looks long, and a blind slice drops the trailing video ID (and can
cut a multibyte character in half).

``fit_filename`` shrinks only the title part so that the whole name is at most
``max_bytes`` bytes, keeps the full video ID and everything after it (lang /
subs-type / LLM suffix / extension), and never splits a code point.
"""
from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple, Optional

MAX_FILENAME_BYTES = 255  # hard filesystem cap (bytes, measured as max(NFC, NFD))
# Target for generated names. Headroom below the hard cap so that suffixes added later
# (iCloud/Drive conflict copies such as " 2" / " (1)", fs_transport's ``.tmp``) can never
# push a name past 255 and cut the video ID.
FILENAME_BUDGET_BYTES = 240
ELLIPSIS = "…"


def _blen(s: str) -> int:
    """Byte length that is safe on every storage layer: max(NFC, NFD) UTF-8 bytes.

    The note is written in its current (NFC) form, but iCloud/Obsidian storage applies the
    255-byte limit to the decomposed (NFD) form, where each Hangul syllable costs 6-8
    bytes instead of 3 (SUE-1298 follow-up). Budgeting on the larger of the two keeps the
    name valid whichever form the filesystem measures.
    """
    return max(
        len(unicodedata.normalize("NFC", s).encode("utf-8")),
        len(unicodedata.normalize("NFD", s).encode("utf-8")),
    )


def truncate_to_bytes(text: str, max_bytes: int, ellipsis: str = ELLIPSIS) -> str:
    """Cut ``text`` to <= max_bytes UTF-8 bytes on a character boundary.

    Appends ``ellipsis`` only when the text was cut and the ellipsis fits.
    Combining marks / ZWJ / variation selectors left dangling at the cut are
    dropped so an emoji sequence is not left half-formed.
    """
    if max_bytes <= 0:
        return ""
    if _blen(text) <= max_bytes:
        return text
    budget = max_bytes - _blen(ellipsis)
    use_ellipsis = budget >= 0 and _blen(ellipsis) <= max_bytes
    if not use_ellipsis:
        budget = max_bytes
    out = []
    for ch in text:
        if _blen("".join(out) + ch) > budget:
            break
        out.append(ch)
    # Do not end on a joiner / dangling modifier from a cut grapheme cluster.
    while out and (out[-1] in ("‍", "️") or unicodedata.combining(out[-1])):
        out.pop()
    cut = "".join(out).rstrip(" ._-")
    if use_ellipsis and cut:
        return cut + ellipsis
    return cut


def fit_filename(
    name: str,
    video_id: str = "",
    *,
    protect_prefix: str = "",
    max_bytes: int = FILENAME_BUDGET_BYTES,
) -> str:
    """Return ``name`` shortened (title only) to <= ``max_bytes`` UTF-8 bytes.

    Layout assumed: ``{protect_prefix}{title}_{video_id}{tail}`` where ``tail`` holds
    lang/type/suffix/extension. Everything from ``video_id`` onward is kept
    verbatim; ``protect_prefix`` (e.g. the ``channel_`` prefix) is kept unless the
    ID and tail alone leave no room for it. Names already within the limit are
    returned unchanged (existing notes are never renamed).
    """
    if _blen(name) <= max_bytes:
        return name

    idx = name.rfind(video_id) if video_id else -1
    if idx >= 0:
        head, tail = name[:idx], name[idx:]
    else:
        # No ID in the name: keep the extension only.
        dot = name.rfind(".")
        head, tail = (name[:dot], name[dot:]) if dot > 0 else (name, "")

    sep = "_" if head.endswith("_") and idx >= 0 else ""
    if sep:
        head = head[:-1]
    prefix = protect_prefix if head.startswith(protect_prefix) else ""
    title = head[len(prefix):]
    # A title that was already shortened once must not stack ellipses.
    title = title.rstrip(ELLIPSIS)

    fixed = _blen(tail) + _blen(sep)
    room = max_bytes - fixed - _blen(prefix)
    if room >= _blen(ELLIPSIS) or (room > 0 and not title):
        new_title = truncate_to_bytes(title, room) if room > 0 else ""
        out = prefix + new_title + sep + tail
    else:
        # Prefix + ID/tail alone do not fit with any title: squeeze the prefix.
        pre_room = max_bytes - fixed
        out = truncate_to_bytes(prefix, pre_room, ellipsis="") + sep + tail
    # Defensive: the tail itself (ID + suffix + ext) must always survive.
    return out


# LLM output suffixes main.py produces: MAIN_LLM_OUTPUT_SUFFIX defaults ("5-mini", "dS4f")
# and ``luna-<DIRECT_LLM_REASONING_EFFORT>`` (low | medium | high, see config.py).
KNOWN_LLM_SUFFIXES = frozenset({"5-mini", "dS4f", "luna-low", "luna-medium", "luna-high"})


class NoteNameParts(NamedTuple):
    video_id: str
    lang: str
    subs_type: str   # "auto_subs" | "subs" | "" (older names without it)
    llm_suffix: str  # MAIN_LLM_OUTPUT_SUFFIX, e.g. "5-mini", "dS4f", "luna-low"


# Mirrors how main.py builds a note name: ``{title}_{video_id}{_lang}_{auto_subs|subs}``
# (txt name) + ``_{MAIN_LLM_OUTPUT_SUFFIX}`` + ``.md``. The LLM suffix is configurable, so
# it is matched structurally (no ``_``) rather than from a fixed list.
_NOTE_TAIL_RE = re.compile(
    r"_(?P<vid>[A-Za-z0-9_-]{11})"
    r"_(?P<lang>[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*)"
    r"(?:_(?P<st>auto_subs|subs))?"
    r"_(?P<llm>[A-Za-z0-9][A-Za-z0-9.-]*)\.md$"
)


def parse_note_name(name: str) -> Optional[NoteNameParts]:
    """Parse a complete P03 note name; ``None`` when the ID or suffix is incomplete."""
    m = _NOTE_TAIL_RE.search(unicodedata.normalize("NFC", name))
    if not m:
        return None
    return NoteNameParts(m.group("vid"), m.group("lang"), m.group("st") or "", m.group("llm"))
