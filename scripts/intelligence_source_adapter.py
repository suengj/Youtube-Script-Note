#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bounded, idempotent P03 -> Learning Intelligence source adapter (SUE-733).

Reads the existing index/note_catalog.jsonl surface and emits source-adapter
records for the recurring Daily Intelligence workflow. It does NOT build a
second catalog, does NOT crawl blogs/RSS, and does NOT recursively scan the
historical Markdown corpus: Markdown files are opened only for the optional
--with-summary-hash pass, and only for rows already inside the bounded window.

Contract: docs/INTELLIGENCE-SOURCE-ADAPTER.md

Usage:
  python scripts/intelligence_source_adapter.py --since-days 3 --dry-run
  python scripts/intelligence_source_adapter.py --output /tmp/p03_intel.jsonl
  python scripts/intelligence_source_adapter.py --replay --since-days 7   # no state write
  P03_INTELLIGENCE_ADAPTER=off python scripts/intelligence_source_adapter.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.note_catalog_utils import (  # noqa: E402
    catalog_path,
    index_dir,
    rel_to_abs_md_path,
    resolve_paths,
)

FORMAT_VERSION = "p03-intel-adapter/1"
STATE_FILENAME = "intelligence_adapter_state.json"
DISABLE_ENV = "P03_INTELLIGENCE_ADAPTER"

# How long an already-emitted video id stays in the state ledger. Bounds the
# state file while still covering any realistic late-arriving catalog row.
DEFAULT_LEDGER_RETENTION_DAYS = 30
# Recovery slack: re-scan a few days behind the checkpoint so a row that was
# written to the catalog late is still picked up exactly once.
DEFAULT_LOOKBACK_DAYS = 2


# --------------------------------------------------------------------------
# enable / disable
# --------------------------------------------------------------------------


def adapter_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """The handoff is opt-out. Disabling it must not touch the P03 runtime."""
    raw = (env if env is not None else os.environ).get(DISABLE_ENV, "").strip().lower()
    return raw not in ("off", "0", "false", "disabled", "no")


# --------------------------------------------------------------------------
# state / checkpoint
# --------------------------------------------------------------------------


def state_path(work_path: str, data_root: str) -> str:
    return os.path.join(index_dir(work_path, data_root), STATE_FILENAME)


def empty_state() -> Dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "last_run_at": None,
        "last_transcript_date": None,
        "pending_window_start": None,  # oldest row a --limit run deferred
        "emitted": {},  # vid -> transcript_date
    }


def load_state(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(data, dict) or data.get("format_version") != FORMAT_VERSION:
        # Unknown/older ledger: start clean rather than silently mis-bounding.
        return empty_state()
    data.setdefault("emitted", {})
    data.setdefault("pending_window_start", None)
    return data


def prune_ledger(
    emitted: Dict[str, str],
    newest_date: Optional[str],
    retention_days: int,
    keep_from: Optional[str] = None,
) -> Dict[str, str]:
    """Keep only ids inside the retention window so the state file stays bounded.

    `keep_from` is a hard floor: no id at or after it is ever pruned. Callers pass
    the start of the window actually read, so an id cannot be evicted while it
    remains reachable — otherwise a run that emitted it, then pruned it, would
    emit it again on the next run, forever.

    For a normal run the window is a few days wide and the retention window
    dominates, so the ledger stays bounded. A deliberately wide `--since` widens
    retention to match, which is the caller's own choice.
    """
    if not newest_date:
        return dict(emitted)
    cutoff = shift_date(newest_date, -retention_days)
    if keep_from and keep_from < cutoff:
        cutoff = keep_from
    return {vid: d for vid, d in emitted.items() if d and d >= cutoff}


def write_state(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# date helpers
# --------------------------------------------------------------------------


def shift_date(date_str: str, days: int) -> str:
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d") + timedelta(days=days)
    return dt.strftime("%Y-%m-%d")


def entry_date(entry: Dict[str, Any]) -> str:
    """Processing date is the checkpoint axis; upload date is source metadata."""
    return (entry.get("transcript_date") or "")[:10]


def resolve_window_start(
    state: Dict[str, Any],
    since_days: Optional[int],
    since: Optional[str],
    lookback_days: int,
    today: str,
) -> str:
    """Lower bound of the bounded read. Explicit args win over the checkpoint."""
    if since:
        start = since[:10]
    elif since_days is not None:
        start = shift_date(today, -since_days)
    else:
        last = state.get("last_transcript_date")
        start = shift_date(last, -lookback_days) if last else shift_date(today, -DEFAULT_LOOKBACK_DAYS)

    # A previous run deferred rows because of --limit. That backlog is older
    # than the checkpoint, so the default window would step over it: widen back
    # to where the backlog starts until it has drained.
    pending = state.get("pending_window_start")
    if pending and pending < start:
        return pending
    return start


# --------------------------------------------------------------------------
# catalog read (streaming, bounded)
# --------------------------------------------------------------------------


def iter_catalog_rows(path: str) -> Iterator[Dict[str, Any]]:
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def select_rows(
    rows: Iterable[Dict[str, Any]], window_start: str, window_end: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Bounded window selection, newest-first, one row per durable video id.

    A video id can appear more than once in the catalog (different language or
    summariser suffix). Discovery only needs one record per video, so the most
    recently processed row wins and the rest are dropped before emission.
    """
    best: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for row in rows:
        vid = (row.get("vid") or "").strip()
        if not vid:
            continue
        d = entry_date(row)
        if not d or d < window_start:
            continue
        if window_end and d > window_end:
            continue
        if not (row.get("source_url") or "").strip():
            continue
        prev = best.get(vid)
        if prev is None or _row_rank(d, row) > _row_rank(prev[0], prev[1]):
            best[vid] = (d, row)
    selected = [row for _, row in best.values()]
    selected.sort(key=lambda r: (entry_date(r), r.get("vid") or ""), reverse=True)
    return selected


def _row_rank(date_str: str, row: Dict[str, Any]) -> Tuple[str, int, int]:
    """Prefer the newest row, then one that actually carries discovery text."""
    return (date_str, 1 if (row.get("tldr") or "").strip() else 0, 1 if row.get("md_path_rel") else 0)


# --------------------------------------------------------------------------
# record mapping
# --------------------------------------------------------------------------


def normalize_tags(raw: Any) -> List[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for t in raw:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return out


def summary_hash_for(md_root: str, rel: str) -> Optional[str]:
    """SHA-256 of the finalized Markdown, for integrity only. Opt-in."""
    if not rel:
        return None
    abs_path = rel_to_abs_md_path(rel, md_root)
    if not abs_path or not os.path.isfile(abs_path):
        return None
    h = hashlib.sha256()
    try:
        with open(abs_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return f"sha256:{h.hexdigest()}"


def to_record(
    row: Dict[str, Any], md_root: str = "", with_summary_hash: bool = False
) -> Dict[str, Any]:
    """Map an existing catalog row onto the documented adapter contract.

    Field names are remapped, not re-derived: this is a compatibility view over
    P03 state, so provenance stays traceable to the original catalog row.
    """
    rel = row.get("md_path_rel") or ""
    record: Dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "source_type": "youtube",
        "video_id": row.get("vid") or "",
        "source_url": row.get("source_url") or "",
        "channel": row.get("channel") or "",
        "title": row.get("title") or "",
        "upload_date": (row.get("upload_date") or "")[:10] or None,
        "processed_at": entry_date(row) or None,
        "summary_ref": rel or None,
        "tldr": (row.get("tldr") or "").strip() or None,
        "tags": normalize_tags(row.get("tags")),
        "evidence_role": "derived-summary",
        "provenance": {
            "system": "p03",
            "catalog": "index/note_catalog.jsonl",
            "catalog_source": row.get("source") or None,
            "catalog_schema_version": row.get("schema_version"),
            "lang": row.get("lang") or None,
            "summary_suffix": row.get("suffix") or None,
        },
    }
    if with_summary_hash:
        record["summary_hash"] = summary_hash_for(md_root, rel)
    return record


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def run_adapter(
    catalog_file: str,
    state_file: str,
    md_root: str = "",
    since_days: Optional[int] = None,
    since: Optional[str] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ledger_retention_days: int = DEFAULT_LEDGER_RETENTION_DAYS,
    with_summary_hash: bool = False,
    replay: bool = False,
    limit: Optional[int] = None,
    today: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Emit adapter records for the bounded window.

    Normal mode is idempotent: ids already in the ledger are skipped, so a
    second run over an unchanged catalog emits nothing. `replay` re-emits the
    whole window and leaves the checkpoint untouched.
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")

    if limit is not None and limit < 1:
        raise ValueError(f"--limit must be at least 1, got {limit}")

    state = load_state(state_file)
    window_start = resolve_window_start(state, since_days, since, lookback_days, today)

    # A missing or unreadable catalog is fail-soft: it must change nothing. In
    # particular it must not look like "the backlog drained".
    catalog_present = os.path.isfile(catalog_file)
    rows = select_rows(iter_catalog_rows(catalog_file), window_start)
    emitted_ledger: Dict[str, str] = dict(state.get("emitted") or {})

    eligible = [
        row for row in rows if replay or (row.get("vid") or "") not in emitted_ledger
    ]
    skipped_known = len(rows) - len(eligible)

    # Rows are newest-first, so a limit drops the *oldest* eligible rows. The
    # checkpoint must not move past them or they would fall outside the next
    # window and never be emitted at all.
    truncated = limit is not None and len(eligible) > limit
    fresh = eligible[:limit] if limit is not None else eligible
    deferred = len(eligible) - len(fresh)

    records = [to_record(r, md_root, with_summary_hash) for r in fresh]

    previous_checkpoint = state.get("last_transcript_date")
    # Oldest row still owed to a consumer: the deferred backlog if this run was
    # truncated, otherwise whatever a previous truncated run left pending.
    carried_pending = state.get("pending_window_start")
    if truncated:
        # A backlog claim only ever moves *older*: a newly deferred row must not
        # overwrite an older claim that is still owed.
        deferred_start = min(entry_date(r) for r in eligible[len(fresh) :])
        pending_start = min([d for d in (carried_pending, deferred_start) if d])
    elif replay or not rows:
        # Nothing drained. The catalog was absent, empty or unreadable — file
        # presence alone is not read success — so an existing claim stands.
        pending_start = carried_pending
    elif carried_pending and not any(entry_date(r) <= carried_pending for r in rows):
        # Rows were read, but none from the backlog region: the deferred row is
        # temporarily absent from the catalog rather than drained. Clearing here
        # would narrow the next window past it and lose it for good.
        pending_start = carried_pending
    else:
        # The backlog region was genuinely covered and everything eligible in it
        # has been emitted.
        pending_start = None

    if truncated:
        # Leave the window open; the ledger already prevents re-emitting what
        # this run sent, so the remainder arrives on the next run.
        newest_overall = previous_checkpoint
    else:
        newest_in_window = max((entry_date(r) for r in rows), default=None)
        newest_overall = max(
            [d for d in (newest_in_window, previous_checkpoint) if d], default=None
        )

    next_state = dict(state)
    if not replay:
        for r in fresh:
            emitted_ledger[r.get("vid") or ""] = entry_date(r)
        next_state = {
            "format_version": FORMAT_VERSION,
            "last_run_at": now,
            "last_transcript_date": newest_overall,
            "pending_window_start": pending_start,
            # Never evict an id this run could still re-read: the floor is the
            # window actually read, which also covers any deferred backlog.
            "emitted": prune_ledger(
                emitted_ledger, newest_overall, ledger_retention_days, keep_from=window_start
            ),
        }

    return {
        "records": records,
        "state": next_state,
        "state_changed": not replay,
        "stats": {
            "window_start": window_start,
            "window_end": today,
            "catalog_rows_in_window": len(rows),
            "emitted": len(records),
            "skipped_already_emitted": skipped_known,
            "deferred_to_next_run": deferred,
            "checkpoint_held_by_limit": truncated,
            "catalog_present": catalog_present,
            "pending_window_start": pending_start,
            "previous_checkpoint": previous_checkpoint,
            "next_checkpoint": next_state.get("last_transcript_date"),
            "ledger_size": len(next_state.get("emitted") or {}),
            "markdown_files_read": len(records) if with_summary_hash else 0,
        },
    }


def write_records(
    records: List[Dict[str, Any]], out_path: str, allow_empty_overwrite: bool = False
) -> Dict[str, Any]:
    """Write the handoff export.

    An empty result is the normal outcome of an idempotent rerun, so it must not
    destroy the export the previous run produced: a consumer pointed at a stable
    path would otherwise silently see zero P03 records. Nothing is written in
    that case unless the caller explicitly asks for it.
    """
    if not records and not allow_empty_overwrite and os.path.exists(out_path):
        return {"path": out_path, "written": False, "status": "kept-existing"}

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = f"{out_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, out_path)
    return {"path": out_path, "written": True, "status": "written"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bounded P03 -> Learning Intelligence source adapter"
    )
    parser.add_argument("--since-days", type=int, default=None)
    parser.add_argument("--since", type=str, default=None, help="YYYY-MM-DD lower bound")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--with-summary-hash", action="store_true")
    parser.add_argument("--replay", action="store_true", help="Re-emit window without advancing state")
    parser.add_argument(
        "--allow-empty-overwrite",
        action="store_true",
        help="Let an empty result replace an existing export (default: keep the previous one)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write records or state")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--work-path", type=str, default=None)
    parser.add_argument("--md-path", type=str, default=None)
    args = parser.parse_args()

    if not adapter_enabled():
        print(f"{DISABLE_ENV} is off; intelligence handoff skipped (P03 runtime unaffected).")
        return 0

    base, work, data_root, md_root = resolve_paths(args.base_path, args.work_path, args.md_path)
    catalog_file = catalog_path(work, data_root)
    state_file = state_path(work, data_root)

    result = run_adapter(
        catalog_file=catalog_file,
        state_file=state_file,
        md_root=md_root,
        since_days=args.since_days,
        since=args.since,
        lookback_days=args.lookback_days,
        with_summary_hash=args.with_summary_hash,
        replay=args.replay,
        limit=args.limit,
    )
    stats = result["stats"]

    print("=" * 60)
    print("P03 intelligence source adapter")
    print("=" * 60)
    print(f"catalog:    {catalog_file}")
    print(f"state:      {state_file}")
    print(f"window:     {stats['window_start']} .. {stats['window_end']}")
    print(f"in window:  {stats['catalog_rows_in_window']} unique videos")
    print(f"emitted:    {stats['emitted']} (skipped already-emitted: {stats['skipped_already_emitted']})")
    if stats["deferred_to_next_run"]:
        print(f"deferred:   {stats['deferred_to_next_run']} row(s) held for the next run (--limit); checkpoint not advanced")
    print(f"checkpoint: {stats['previous_checkpoint']} -> {stats['next_checkpoint']}")
    print(f"ledger:     {stats['ledger_size']} ids")
    print(f"md reads:   {stats['markdown_files_read']}")

    if args.dry_run:
        for rec in result["records"][:5]:
            print(f"  [{rec['video_id']}] {rec['processed_at']} {rec['channel']} :: {rec['title'][:60]}")
        if len(result["records"]) > 5:
            print(f"  ... and {len(result['records']) - 5} more")
        print("(dry-run: no records or state written)")
        return 0

    out_path = args.output or os.path.join(
        index_dir(work, data_root), f"intelligence_source_{datetime.now():%Y%m%d}.jsonl"
    )
    written = write_records(result["records"], out_path, args.allow_empty_overwrite)
    if written["status"] == "kept-existing":
        print(f"records:    {out_path} (unchanged — no new records, previous export kept)")
    else:
        print(f"records:    {out_path}")

    if result["state_changed"]:
        write_state(state_file, result["state"])
        print("state:      advanced")
    else:
        print("state:      unchanged (replay)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
