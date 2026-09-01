#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parse and assemble mobile-friendly MD (Phase 1b)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from scripts.note_catalog_utils import (
    NOTE_CATALOG_SCHEMA_VERSION,
    abs_to_rel_md_path,
    build_frontmatter_yaml,
    extract_lang_from_filename,
    extract_suffix_from_filename,
    extract_vid_from_filename,
)

TAGS_SECTION = re.compile(
    r"(?ms)^## Tags\s*\n(.*?)(?=^\s*## |\Z)",
)
TLDR_SECTION = re.compile(
    r"(?ms)^## 한눈에 보기\s*\n(.*?)(?=^\s*## |\Z|^> \[!note\])",
)
H1_TITLE = re.compile(r"^#\s+(.+)$", re.M)
UPLOAD_DATE_HEADER = re.compile(r"^영상 업로드 일자:\s*\S+\s*\n+", re.M)
CALLOUT_HEADER = re.compile(r"^> \[!note\]-\s*(Insights|Key Takeaways)\s*$", re.M)
BLOCK_BOUNDARY = re.compile(r"^(?:> \[!note\]-|## )")


def normalize_obsidian_callouts(body: str) -> str:
    """
    Ensure Obsidian collapsible callout body lines are blockquoted (`>` prefix).
    Fixes LLM output where only the header line has `>`.
    """
    lines = body.splitlines()
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if CALLOUT_HEADER.match(line.rstrip()):
            out.append(line.rstrip())
            i += 1
            while i < len(lines):
                inner = lines[i]
                if inner.strip() == "":
                    out.append(inner)
                    i += 1
                    continue
                if BLOCK_BOUNDARY.match(inner):
                    break
                if not inner.startswith(">"):
                    stripped = inner.lstrip()
                    out.append("> " + stripped if stripped else ">")
                else:
                    out.append(inner)
                i += 1
            continue
        out.append(line)
        i += 1
    result = "\n".join(out)
    if body.endswith("\n"):
        result += "\n"
    return result


def extract_callout(body: str, label: str) -> str:
    """
    Extract Obsidian callout body for label (e.g. Insights, Key Takeaways).
    Returns plain text bullets (blockquote prefixes stripped), or "" if missing.
    """
    m = re.search(
        rf"^> \[!note\]-\s*{re.escape(label)}\s*\n(.*?)(?=^> \[!note\]-|^## |\Z)",
        body,
        re.M | re.S,
    )
    if not m:
        return ""
    lines: List[str] = []
    for ln in m.group(1).splitlines():
        if not ln.strip():
            continue
        text = ln.lstrip()
        if text.startswith(">"):
            text = text[1:].lstrip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def audit_callout_prefixes(body: str) -> List[str]:
    """Return list of issues: non-empty callout lines missing `>` prefix."""
    issues: List[str] = []
    for label in ("Insights", "Key Takeaways"):
        m = re.search(
            rf"^> \[!note\]- {label}\s*\n(.*?)(?=^> \[!note\]-|^## |\Z)",
            body,
            re.M | re.S,
        )
        if not m:
            continue
        for ln in m.group(1).splitlines():
            if ln.strip() and not ln.startswith(">"):
                issues.append(f"{label}: {ln[:60]}")
    return issues


def _strip_section(body: str, pattern: re.Pattern) -> Tuple[str, str]:
    m = pattern.search(body)
    if not m:
        return body, ""
    extracted = m.group(1).strip()
    new_body = body[: m.start()] + body[m.end() :]
    return new_body.strip(), extracted


def parse_tags_section(body: str) -> Tuple[str, List[str]]:
    """Remove ## Tags section; return cleaned body and tag list."""
    cleaned, raw = _strip_section(body, TAGS_SECTION)
    if not raw:
        return body, []
    tags: List[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s*", "", line)
        line = line.strip("`\"' ")
        if line:
            tags.append(line.lower())
    return cleaned, tags[:5]


def extract_tldr(body: str, max_len: int = 500) -> str:
    _, raw = _strip_section(body, TLDR_SECTION)
    if not raw:
        return ""
    text = re.sub(r"^[-*]\s*", "", raw, flags=re.M)
    text = re.sub(r"\[(?:확정|정황|추정|외부지식)\]\s*", "", text)
    text = " ".join(text.split())
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def extract_title(body: str, fallback: str = "") -> str:
    m = H1_TITLE.search(body)
    if m:
        return m.group(1).strip()
    return fallback.strip()


def prepare_mobile_body(response: str) -> Tuple[str, List[str], str, str]:
    """
    Parse LLM response: strip upload-date header, extract tags/tldr/title.
    Returns (body_without_tags, tags, title, tldr).
    """
    body = UPLOAD_DATE_HEADER.sub("", response.strip())
    body, tags = parse_tags_section(body)
    body = normalize_obsidian_callouts(body)
    title = extract_title(body, fallback="")
    tldr = extract_tldr(body)
    return body, tags, title, tldr


def build_save_entry(
    *,
    md_abs_path: str,
    md_root: str,
    vid: str,
    channel: str,
    upload_date: str,
    transcript_date: str,
    lang: str,
    suffix: str,
    source_url: str,
    tags: Optional[List[str]] = None,
    title: str = "",
    tldr: str = "",
) -> Dict[str, Any]:
    fname = md_abs_path.split("/")[-1] if md_abs_path else ""
    entry: Dict[str, Any] = {
        "schema_version": NOTE_CATALOG_SCHEMA_VERSION,
        "vid": vid or extract_vid_from_filename(fname),
        "md_path_rel": abs_to_rel_md_path(md_abs_path, md_root),
        "channel": channel,
        "upload_date": (upload_date or "")[:10],
        "transcript_date": (transcript_date or "")[:10],
        "lang": lang or extract_lang_from_filename(fname),
        "suffix": suffix or extract_suffix_from_filename(fname),
        "source_url": source_url or (f"https://www.youtube.com/watch?v={vid}" if vid else ""),
        "source": "live_pipeline",
    }
    if tags:
        entry["tags"] = tags
    if title:
        entry["title"] = title
    if tldr:
        entry["tldr"] = tldr
    return entry


def assemble_mobile_md(entry: Dict[str, Any], body: str) -> str:
    """YAML frontmatter (format 4.1) + markdown body."""
    fm_entry = dict(entry)
    fm_entry["format_version"] = "4.1"
    return build_frontmatter_yaml(fm_entry) + body.lstrip("\n")
