#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SUE-1297: add the canonical ``obsidian`` column to channel_df.csv (non-destructive).

  python scripts/migrate_channel_df_obsidian.py inspect   # dry-run: rowcount, header before/after, no-op?
  python scripts/migrate_channel_df_obsidian.py apply     # lock -> backup -> add column -> verify -> atomic replace

The file is resolved from the repo's DATA_ROOT config (``--data-root`` overrides, for tests).
Existing rows/order/columns/cursors are untouched; ``Obsidian`` is folded into ``obsidian``;
conflicting values or duplicate headers abort with an error. A re-run is a no-op.
Refuses to run while the processor holds the run lock. Never prints channel names.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import channel_crawl as cc  # noqa: E402
import run_lock  # noqa: E402


def plan(path: str) -> Dict:
    before_header, rows = cc.read_channel_df_raw(path)
    after_header = list(before_header)
    if cc.OBSIDIAN_COLUMN not in after_header:
        after_header.append(cc.OBSIDIAN_COLUMN)
    raw_first_line = open(path, "r", encoding="utf-8-sig").readline().rstrip("\r\n")
    header_changed = raw_first_line.split(",") != after_header
    return {
        "rowcount": len(rows),
        "header_before": raw_first_line.split(","),
        "header_after": after_header,
        "noop": not header_changed,
        "rows": rows,
    }


def _verify(tmp_path: str, orig_rows: List[Dict[str, str]], after_header: List[str]) -> None:
    header, new_rows = cc.read_channel_df_raw(tmp_path)
    if header != after_header:
        raise RuntimeError("verify: header mismatch after write")
    if len(new_rows) != len(orig_rows):
        raise RuntimeError(f"verify: rowcount {len(orig_rows)} -> {len(new_rows)}")
    for i, (a, b) in enumerate(zip(orig_rows, new_rows)):
        for k, v in a.items():
            if b.get(k) != v:
                raise RuntimeError(f"verify: row {i} column {k!r} changed")


def apply(path: str, lock_dir: Optional[str]) -> Dict:
    handle = None
    if lock_dir:
        ok, handle, msg = run_lock.acquire_run_lock(lock_dir, "manual", True)
        if not ok:
            raise SystemExit(f"refused: processor holds the run lock ({msg})")
    try:
        p = plan(path)
        if p["noop"]:
            return {**p, "applied": False}
        backup = f"{path}.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(path, backup)
        tmp_path = path + ".migrate.tmp"
        cc.write_channel_df_atomic(tmp_path, p["header_after"], p["rows"])
        try:
            _verify(tmp_path, p["rows"], p["header_after"])
        except Exception:
            os.remove(tmp_path)
            raise
        os.replace(tmp_path, path)
        return {**p, "applied": True, "backup": backup}
    finally:
        if handle is not None:
            handle.release()


def _default_dirs():
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
    from config import resolve_data_root
    base = os.getenv("BASE_PATH", str(_ROOT))
    work = os.getenv("WORK_PATH", "").strip() or None
    return resolve_data_root(base, work), base


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("command", choices=["inspect", "apply"])
    ap.add_argument("--data-root", default=None, help="override DATA_ROOT (tests)")
    ap.add_argument("--lock-dir", default=None, help="override run-lock dir (default BASE_PATH, as main.py)")
    a = ap.parse_args(argv)
    if a.data_root:
        data_root, lock_dir = a.data_root, a.lock_dir or a.data_root
    else:
        data_root, base = _default_dirs()
        lock_dir = a.lock_dir or base
    path = os.path.join(data_root, cc.CHANNEL_DF_FILENAME)
    if not os.path.exists(path):
        print(f"error: {path} not found", file=sys.stderr)
        return 2
    try:
        res = plan(path) if a.command == "inspect" else apply(path, lock_dir)
    except cc.ChannelDfSchemaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    print(f"file: {path}")
    print(f"rowcount: {res['rowcount']}")
    print(f"header_before: {','.join(res['header_before'])}")
    print(f"header_after: {','.join(res['header_after'])}")
    print(f"noop: {res['noop']}")
    if a.command == "apply":
        print(f"applied: {res['applied']}" + (f" backup: {res['backup']}" if res.get("backup") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
