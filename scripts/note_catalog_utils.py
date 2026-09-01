#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for note_catalog build, audit, and frontmatter backfill."""

from __future__ import annotations

import csv
import json
import os
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

try:
    from config import NOTE_CATALOG_SCHEMA_VERSION, resolve_data_root
except ImportError:
    NOTE_CATALOG_SCHEMA_VERSION = 1

    def resolve_data_root(base_path: str, work_path=None) -> str:
        return os.path.abspath(base_path)

DATE_FOLDER_PATTERN = re.compile(r"^\d{4}_\d{2}_\d{2}$")
VID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
VID_BEFORE_SUFFIX = re.compile(
    r"_([A-Za-z0-9_-]{11})_(?:"
    r"ko-orig|en-orig|jp-orig|ja-orig|"
    r"ko_auto_subs|en_auto_subs|auto_subs|"
    r"ko_subs|en_subs|subs|"
    r"5-mini|5-mi|dS4f|grok|o4-mini|o1-mini|o3-mini|mini"
    r")",
    re.I,
)
LANG_IN_NAME = re.compile(r"_(ko|en|jp|ja)(?:-orig|-auto_subs|_subs|_auto_subs)?(?:_|\.md)", re.I)
SUFFIX_IN_NAME = re.compile(
    r"_(5-mini|5-mi|dS4f|grok|o4-mini|o1-mini|o3-mini|mini)(?:\.md)?$", re.I
)
DEFAULT_MD_PATH = ""


def load_dotenv_project() -> None:
    try:
        from dotenv import load_dotenv

        root = Path(__file__).resolve().parents[1]
        load_dotenv(root / ".env")
    except Exception:
        pass


def resolve_paths(
    base_path: Optional[str] = None,
    work_path: Optional[str] = None,
    md_path: Optional[str] = None,
) -> Tuple[str, str, str, str]:
    """Return (base_path, work_path, data_root, md_path)."""
    load_dotenv_project()
    base = (base_path or os.getenv("BASE_PATH", "")).strip()
    if not base:
        base = str(Path(__file__).resolve().parents[1])
    base = os.path.abspath(base)
    work = (work_path or os.getenv("WORK_PATH", "")).strip() or None
    data_root = resolve_data_root(base, work)
    md = (md_path or os.getenv("OUTPUT_MD_PATH", "")).strip() or DEFAULT_MD_PATH
    if not md:
        raise ValueError(
            "OUTPUT_MD_PATH must be set in .env (Markdown output directory)"
        )
    md = os.path.abspath(md)
    return base, work or "", data_root, md


def index_dir(work_path: str, data_root: str) -> str:
    if work_path and os.path.isdir(work_path):
        path = os.path.join(work_path, "index")
    else:
        path = os.path.join(data_root, "index")
    os.makedirs(path, exist_ok=True)
    return path


def catalog_path(work_path: str, data_root: str) -> str:
    return os.path.join(index_dir(work_path, data_root), "note_catalog.jsonl")


def audit_report_path(work_path: str, data_root: str) -> str:
    return os.path.join(index_dir(work_path, data_root), "note_catalog_audit.json")


def iter_jsonl(path: str) -> Iterator[Dict[str, Any]]:
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def normalize_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return value
    m = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$", value)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return value[:10] if len(value) >= 10 else value


def abs_to_rel_md_path(md_abs: str, md_root: str) -> str:
    if not md_abs:
        return ""
    md_abs = os.path.normpath(md_abs)
    md_root = os.path.normpath(md_root)
    if md_abs.startswith(md_root + os.sep):
        return md_abs[len(md_root) + 1 :].replace("\\", "/")
    marker = "002_YT_Script"
    if marker in md_abs:
        idx = md_abs.index(marker) + len(marker) + 1
        return md_abs[idx:].replace("\\", "/")
    return os.path.basename(md_abs)


def rel_to_abs_md_path(md_rel: str, md_root: str) -> str:
    if not md_rel:
        return ""
    return os.path.normpath(os.path.join(md_root, md_rel.replace("/", os.sep)))


def extract_vid_from_filename(name: str) -> str:
    base = Path(name).stem
    m = VID_BEFORE_SUFFIX.search(base)
    if m:
        return m.group(1)
    if "+vid-" in base:
        m2 = re.search(r"\+vid-([A-Za-z0-9_-]{11})", base)
        if m2:
            return m2.group(1)
    matches = VID_PATTERN.findall(base)
    for cand in reversed(matches):
        if len(cand) == 11 and not cand.startswith(("ko-orig", "en-orig")):
            return cand
    return ""


def extract_channel_from_filename(name: str) -> str:
    stem = Path(name).stem
    if stem.startswith("_"):
        return ""
    if "_" not in stem:
        return ""
    prefix = stem.split("_", 1)[0]
    if not prefix or VID_PATTERN.fullmatch(prefix):
        return ""
    return prefix


def extract_lang_from_filename(name: str) -> str:
    m = LANG_IN_NAME.search(name)
    if not m:
        return ""
    lang = m.group(1).lower()
    return "ja" if lang == "jp" else lang


def extract_suffix_from_filename(name: str) -> str:
    m = SUFFIX_IN_NAME.search(name)
    return m.group(1) if m else ""


def parse_date_folder(folder_name: str) -> Optional[datetime]:
    if not DATE_FOLDER_PATTERN.match(folder_name):
        return None
    y, mo, d = folder_name.split("_")
    try:
        return datetime(int(y), int(mo), int(d))
    except ValueError:
        return None


def iter_md_files(md_root: str, since: Optional[datetime] = None) -> Iterator[Tuple[str, str]]:
    """Yield (relative_path, absolute_path) under md_root, optional date-folder filter."""
    root = Path(md_root)
    if not root.is_dir():
        return
    for path in root.rglob("*.md"):
        if path.name.startswith("."):
            continue
        rel = path.relative_to(root).as_posix()
        if since is not None:
            parent = path.parent.name
            dt = parse_date_folder(parent)
            if dt is None or dt < since:
                continue
        yield rel, str(path)


def entry_from_record(
    rec: Dict[str, Any],
    md_root: str,
    source: str,
    url_by_vid: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    vid = (rec.get("v_id") or rec.get("vid") or "").strip()
    md_abs = (rec.get("md_path") or "").strip()
    md_rel = abs_to_rel_md_path(md_abs, md_root) if md_abs else ""
    if not md_rel and not vid:
        return None
    fname = os.path.basename(md_rel) if md_rel else ""
    upload_date = normalize_date(rec.get("upload_date") or "")
    transcript_date = normalize_date(rec.get("transcript_date") or "")
    channel = extract_channel_from_filename(fname)
    url = (url_by_vid or {}).get(vid, "")
    if not url and vid:
        url = f"https://www.youtube.com/watch?v={vid}"
    return {
        "schema_version": NOTE_CATALOG_SCHEMA_VERSION,
        "vid": vid,
        "md_path_rel": md_rel,
        "channel": channel,
        "upload_date": upload_date,
        "transcript_date": transcript_date,
        "method": (rec.get("method") or "").strip(),
        "has_yid": bool(rec.get("has_yid", bool(vid))),
        "lang": extract_lang_from_filename(fname),
        "suffix": extract_suffix_from_filename(fname),
        "source_url": url,
        "source": source,
    }


def load_url_by_vid_from_output_df(data_root: str) -> Dict[str, str]:
    path = os.path.join(data_root, "output_df_new.csv")
    out: Dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            vid = (row.get("v_id") or "").strip()
            url = (row.get("url") or "").strip()
            if vid and url and vid not in out:
                out[vid] = url
    return out


def merge_catalog_entries(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_vid: Dict[str, Dict[str, Any]] = {}
    by_rel: Dict[str, Dict[str, Any]] = {}

    def score(e: Dict[str, Any]) -> Tuple[int, int]:
        s = 0
        if e.get("md_path_rel"):
            s += 4
        if e.get("upload_date"):
            s += 2
        if e.get("vid"):
            s += 2
        if e.get("source") == "live_jsonl":
            s += 1
        return (s, len(json.dumps(e, ensure_ascii=False)))

    for entry in entries:
        vid = entry.get("vid") or ""
        rel = entry.get("md_path_rel") or ""
        if vid:
            prev = by_vid.get(vid)
            if prev is None or score(entry) >= score(prev):
                by_vid[vid] = entry
        if rel:
            prev = by_rel.get(rel)
            if prev is None or score(entry) >= score(prev):
                by_rel[rel] = entry

    merged: Dict[str, Dict[str, Any]] = {}
    for e in by_vid.values():
        key = e.get("vid") or e.get("md_path_rel")
        if key:
            merged[key] = e
    for rel, e in by_rel.items():
        key = e.get("vid") or rel
        if key not in merged or score(e) > score(merged[key]):
            merged[key] = e
    return sorted(
        merged.values(),
        key=lambda x: (x.get("transcript_date") or x.get("upload_date") or "", x.get("md_path_rel") or ""),
    )


def build_catalog(base_path: str, work_path: str, md_root: str) -> List[Dict[str, Any]]:
    data_root = resolve_data_root(base_path, work_path or None)
    url_by_vid = load_url_by_vid_from_output_df(data_root)
    entries: List[Dict[str, Any]] = []

    for name in ("video_metadata_merged.jsonl", "video_metadata_offline.jsonl"):
        path = os.path.join(data_root, name)
        for rec in iter_jsonl(path):
            e = entry_from_record(rec, md_root, source=name.replace(".jsonl", ""), url_by_vid=url_by_vid)
            if e:
                entries.append(e)

    live_path = os.path.join(data_root, "video_metadata_live.jsonl")
    for rec in iter_jsonl(live_path):
        e = entry_from_record(rec, md_root, source="live_jsonl", url_by_vid=url_by_vid)
        if e:
            entries.append(e)

    # output_df success rows without md_path
    out_csv = os.path.join(data_root, "output_df_new.csv")
    if os.path.isfile(out_csv):
        with open(out_csv, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                vid = (row.get("v_id") or "").strip()
                if not vid:
                    continue
                status = (row.get("status") or row.get("results") or "").strip().lower()
                if status not in ("success", "unknown"):
                    continue
                entries.append(
                    {
                        "schema_version": NOTE_CATALOG_SCHEMA_VERSION,
                        "vid": vid,
                        "md_path_rel": "",
                        "channel": "",
                        "upload_date": "",
                        "transcript_date": normalize_date(row.get("date") or ""),
                        "method": "",
                        "has_yid": True,
                        "lang": "",
                        "suffix": "",
                        "source_url": (row.get("url") or "").strip(),
                        "source": "output_df",
                    }
                )

    return merge_catalog_entries(entries)


def write_catalog(entries: List[Dict[str, Any]], out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, out_path)


def load_catalog(path: str) -> List[Dict[str, Any]]:
    return list(iter_jsonl(path))


def has_frontmatter(content: str) -> bool:
    return content.lstrip("\ufeff").startswith("---")


def strip_leading_frontmatter(content: str) -> str:
    text = content.lstrip("\ufeff")
    if not text.startswith("---"):
        return content
    parts = text.split("---", 2)
    if len(parts) < 3:
        return content
    return parts[2].lstrip("\n")


def build_frontmatter_yaml(entry: Dict[str, Any]) -> str:
    import yaml

    data: Dict[str, Any] = {
        "format_version": entry.get("format_version") or "4.0",
    }
    for key in (
        "vid",
        "channel",
        "upload_date",
        "transcript_date",
        "lang",
        "suffix",
        "source_url",
        "title",
        "tldr",
    ):
        val = entry.get(key)
        if val:
            data[key] = val
    tags = entry.get("tags")
    if tags:
        data["tags"] = list(tags)[:5]
    if entry.get("has_yid") is False:
        data["has_yid"] = False
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{body}\n---\n\n"


def append_catalog_entry(work_path: str, data_root: str, entry: Dict[str, Any]) -> None:
    """Append one row to note_catalog.jsonl (live pipeline)."""
    path = catalog_path(work_path, data_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    tmp = path + ".append.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as src:
                f.write(src.read())
        f.write(line)
    os.replace(tmp, path)


def atomic_write_text_with_retry(
    final_path: str,
    content: str,
    *,
    encoding: str = "utf-8-sig",
    max_attempts: int = 8,
) -> None:
    directory = os.path.dirname(os.path.abspath(final_path))
    os.makedirs(directory, exist_ok=True)
    base_name = os.path.basename(final_path)
    tmp_path = os.path.join(directory, f".{base_name}.{os.getpid()}.tmp")
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            with open(tmp_path, "w", encoding=encoding) as f:
                f.write(content)
            os.replace(tmp_path, final_path)
            return
        except OSError as e:
            last_exc = e
            n = getattr(e, "errno", None)
            if n == 28:
                raise
            if n == 11 or "errno 11" in str(e).lower():
                if attempt < max_attempts - 1:
                    import time

                    time.sleep(0.45 * (attempt + 1) + random.uniform(0, 0.35))
                    continue
            raise
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    if last_exc:
        raise last_exc


def catalog_lookup_by_rel(catalog: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for e in catalog:
        rel = e.get("md_path_rel") or ""
        if rel:
            out[rel] = e
    return out


def catalog_lookup_by_vid(catalog: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for e in catalog:
        vid = e.get("vid") or ""
        if vid:
            out[vid] = e
    return out


def enrich_entry_from_path(rel: str, abs_path: str, entry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fname = os.path.basename(rel)
    base = dict(entry or {})
    base.setdefault("schema_version", NOTE_CATALOG_SCHEMA_VERSION)
    base.setdefault("md_path_rel", rel)
    base.setdefault("vid", extract_vid_from_filename(fname))
    base.setdefault("channel", extract_channel_from_filename(fname))
    base.setdefault("lang", extract_lang_from_filename(fname))
    base.setdefault("suffix", extract_suffix_from_filename(fname))
    if base.get("vid") and not base.get("source_url"):
        base["source_url"] = f"https://www.youtube.com/watch?v={base['vid']}"
    dt = parse_date_folder(Path(rel).parent.name)
    if dt and not base.get("transcript_date"):
        base["transcript_date"] = dt.strftime("%Y-%m-%d")
    return base


def recent_cutoff(days: int) -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
