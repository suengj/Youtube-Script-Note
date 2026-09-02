#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Export content candidates from note_catalog to CSV (no LLM).

Usage:
  python scripts/export_blog_candidates.py --days 7 --tags ai,crypto,llm
  python scripts/export_blog_candidates.py --days 14 --tags ai --dry-run
  python scripts/export_blog_candidates.py --output /path/blog_candidates.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.md_mobile_utils import (  # noqa: E402
    extract_callout,
    extract_tldr,
    extract_title,
    parse_tags_section,
)
from scripts.note_catalog_utils import (  # noqa: E402
    catalog_path,
    has_frontmatter,
    index_dir,
    load_catalog,
    rel_to_abs_md_path,
    resolve_paths,
    strip_leading_frontmatter,
)

# CSV columns for downstream publishing workflows (topic, keywords, context, etc.)
CSV_FIELDS = [
    "group_id_hint",
    "group_name",
    "topic",
    "keywords",
    "category",
    "context_summary",
    "source",
    "message_hierarchy",
    "vid",
    "channel",
    "upload_date",
    "transcript_date",
    "md_path_rel",
    "serp_site_list",
]

DEFAULT_SERP = "aitimes.com,zdnet.co.kr,techcrunch.com,reuters.com,bloomberg.com"
TAG_TO_CATEGORY = {
    "crypto": "경제",
    "solana": "경제",
    "fintech": "경제",
    "bitcoin": "경제",
    "investment": "경제",
    "stock": "경제",
    "ai": "기술",
    "llm": "기술",
    "agent": "기술",
    "openai": "기술",
}


def parse_tags_arg(raw: str) -> Set[str]:
    return {t.strip().lower() for t in (raw or "").split(",") if t.strip()}


def entry_date(entry: Dict[str, Any]) -> str:
    return (entry.get("transcript_date") or entry.get("upload_date") or "")[:10]


def entry_tags(entry: Dict[str, Any]) -> List[str]:
    tags = entry.get("tags") or []
    if isinstance(tags, str):
        return [tags.lower()]
    return [str(t).lower() for t in tags]


def enrich_tags_from_md(entry: Dict[str, Any], md_root: str) -> List[str]:
    existing = entry_tags(entry)
    if existing:
        return existing
    rel = entry.get("md_path_rel") or ""
    if not rel:
        return []
    abs_path = rel_to_abs_md_path(rel, md_root)
    if not os.path.isfile(abs_path):
        return []
    try:
        with open(abs_path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    except OSError:
        return []
    body = strip_leading_frontmatter(raw) if has_frontmatter(raw) else raw
    _, tags = parse_tags_section(body)
    return tags


def tags_match(entry: Dict[str, Any], filter_tags: Set[str], md_root: str, loose: bool) -> bool:
    if not filter_tags:
        return True
    et = set(enrich_tags_from_md(entry, md_root))
    if et & filter_tags:
        return True
    if not loose:
        return False
    hay = " ".join(
        [
            entry.get("title") or "",
            entry.get("tldr") or "",
            entry.get("channel") or "",
            entry.get("md_path_rel") or "",
        ]
    ).lower()
    return any(t in hay for t in filter_tags)


def infer_category(entry: Dict[str, Any]) -> str:
    for t in entry_tags(entry):
        if t in TAG_TO_CATEGORY:
            return TAG_TO_CATEGORY[t]
    ch = (entry.get("channel") or "").lower()
    if any(x in ch for x in ("uncle", "crypto", "머니", "월가", "경제")):
        return "경제"
    return "기술"


def load_md_body(md_root: str, rel: str) -> str:
    abs_path = rel_to_abs_md_path(rel, md_root)
    if not abs_path or not os.path.isfile(abs_path):
        return ""
    try:
        with open(abs_path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    except OSError:
        return ""
    return strip_leading_frontmatter(raw) if has_frontmatter(raw) else raw


def load_md_snippet(md_root: str, rel: str, max_chars: int = 1200) -> str:
    body = load_md_body(md_root, rel)
    if not body:
        return ""
    tldr = extract_tldr(body)
    if tldr:
        return tldr
    return body[:max_chars].strip()


def _append_callout_section(lines: List[str], heading: str, callout_text: str) -> None:
    if not callout_text.strip():
        return
    lines.append("")
    lines.append(heading)
    for ln in callout_text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if not ln.startswith("-"):
            ln = f"- {ln}"
        lines.append(ln)


def build_context_summary(entry: Dict[str, Any], md_root: str) -> str:
    title = entry.get("title") or ""
    tldr = entry.get("tldr") or ""
    rel = entry.get("md_path_rel") or ""
    channel = entry.get("channel") or ""
    url = entry.get("source_url") or ""

    body = load_md_body(md_root, rel) if rel else ""
    if not tldr:
        tldr = extract_tldr(body) if body else ""
        if not tldr and rel:
            tldr = load_md_snippet(md_root, rel)

    insights = extract_callout(body, "Insights") if body else ""
    takeaways = extract_callout(body, "Key Takeaways") if body else ""

    lines = []
    if title:
        lines.append(f"{title} 영상을 핵심 논지로 설정할 것.")
    if tldr:
        lines.append("")
        lines.append("[확정/정황]")
        for part in tldr.replace("…", "").split(". ")[:3]:
            part = part.strip()
            if part:
                lines.append(f"- {part}")
    if insights:
        _append_callout_section(lines, "[해석]", insights)
    else:
        lines.append("")
        lines.append("[해석] 추정·Insights는 별도 섹션. SERP 교차검증.")
    if takeaways:
        _append_callout_section(lines, "[시사점]", takeaways)
    if url:
        lines.append(f"참고: {url}" + (f" ({channel})" if channel else ""))
    return "\n".join(lines)


def row_from_entry(entry: Dict[str, Any], md_root: str, seq: int, date_prefix: str) -> Dict[str, str]:
    tags = enrich_tags_from_md(entry, md_root) or entry_tags(entry)
    keywords = ", ".join(tags) if tags else (entry.get("channel") or "youtube")
    topic = entry.get("title") or entry.get("tldr") or entry.get("vid") or "Untitled"
    if len(topic) > 120:
        topic = topic[:117] + "..."

    return {
        "group_id_hint": f"{date_prefix}{seq:02d}",
        "group_name": "YT Script",
        "topic": topic,
        "keywords": keywords,
        "category": infer_category(entry),
        "context_summary": build_context_summary(entry, md_root),
        "source": entry.get("source_url") or "",
        "message_hierarchy": "메인",
        "vid": entry.get("vid") or "",
        "channel": entry.get("channel") or "",
        "upload_date": (entry.get("upload_date") or "")[:10],
        "transcript_date": (entry.get("transcript_date") or "")[:10],
        "md_path_rel": entry.get("md_path_rel") or "",
        "serp_site_list": DEFAULT_SERP,
    }


def filter_entries(
    catalog: List[Dict[str, Any]],
    since: datetime,
    filter_tags: Set[str],
    channel: Optional[str] = None,
    md_root: str = "",
    loose: bool = False,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen_vid: Set[str] = set()
    for e in catalog:
        if not e.get("md_path_rel"):
            continue
        d = entry_date(e)
        if not d:
            continue
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
        if dt < since:
            continue
        if not tags_match(e, filter_tags, md_root, loose):
            continue
        if channel and channel.lower() not in (e.get("channel") or "").lower():
            continue
        vid = e.get("vid") or e.get("md_path_rel")
        if vid in seen_vid:
            continue
        seen_vid.add(vid)
        out.append(e)
    out.sort(key=lambda x: (entry_date(x), x.get("channel") or ""), reverse=True)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Export blog candidates from note_catalog")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--tags", type=str, default="", help="Comma-separated tag filter (any match)")
    parser.add_argument("--channel", type=str, default="", help="Channel substring filter")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--work-path", type=str, default=None)
    parser.add_argument("--md-path", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--loose", action="store_true", help="Also match title/channel/path substring")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    base, work, data_root, md_root = resolve_paths(args.base_path, args.work_path, args.md_path)
    cat_file = catalog_path(work, data_root)
    catalog = load_catalog(cat_file) if os.path.isfile(cat_file) else []

    since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=args.days)
    filter_tags = parse_tags_arg(args.tags)
    channel = args.channel.strip() or None
    entries = filter_entries(catalog, since, filter_tags, channel, md_root, args.loose)

    today = datetime.now().strftime("%Y%m%d")
    out_path = args.output or os.path.join(index_dir(work, data_root), f"blog_candidates_{today}.csv")

    print("=" * 60)
    print("Export blog candidates")
    print("=" * 60)
    print(f"catalog:  {cat_file} ({len(catalog)} rows)")
    print(f"since:    {since.date()} ({args.days} days)")
    print(f"tags:     {sorted(filter_tags) if filter_tags else '(all)'}")
    print(f"channel:  {channel or '(all)'}")
    print(f"matched:  {len(entries)}")
    print(f"output:   {out_path}")

    rows = [
        row_from_entry(e, md_root, i + 1, today)
        for i, e in enumerate(entries)
    ]

    if args.dry_run:
        for r in rows[:5]:
            print(f"  [{r['group_id_hint']}] {r['topic'][:50]} | {r['category']} | {r['vid']}")
        if len(rows) > 5:
            print(f"  ... and {len(rows) - 5} more")
        return

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows -> {out_path}")
    print("Next: review the CSV and import into your downstream publishing workflow.")


if __name__ == "__main__":
    main()
