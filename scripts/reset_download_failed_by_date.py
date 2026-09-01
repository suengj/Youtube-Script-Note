#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Remove output_df rows for a given run date + status=download_failed, and reset
matching rows in crawl_yt_list.csv to queued (retry_count=0) so channel crawl
will pick them up again.

Example (dry-run):
  python scripts/reset_download_failed_by_date.py --date 2026-05-09 --dry-run

Apply:
  python scripts/reset_download_failed_by_date.py --date 2026-05-09

Default --data-root: env DATA_ROOT or WORK_PATH/data or ./data under cwd.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path

import pandas as pd


def _video_id_from_watch_url(url: str) -> str:
    if not url or not isinstance(url, str):
        return ""
    m = re.search(r"[?&]v=([^&]+)", url.strip())
    return m.group(1).strip() if m else ""


def _default_data_root() -> Path:
    dr = (os.environ.get("DATA_ROOT") or "").strip()
    if dr:
        return Path(dr).expanduser()
    wp = (os.environ.get("WORK_PATH") or "").strip()
    if wp:
        return Path(wp).expanduser() / "data"
    return Path.cwd() / "data"


def main() -> None:
    ap = argparse.ArgumentParser(description="Reset download_failed for one output_df date + crawl queue.")
    ap.add_argument("--date", required=True, help='output_df date column value, e.g. 2026-05-09')
    ap.add_argument("--data-root", type=Path, default=None, help="Directory with output_df_new.csv and crawl_yt_list.csv")
    ap.add_argument("--dry-run", action="store_true", help="Print counts only; do not write files")
    args = ap.parse_args()

    data_root = Path(args.data_root).expanduser() if args.data_root else _default_data_root()
    out_path = data_root / "output_df_new.csv"
    queue_path = data_root / "crawl_yt_list.csv"
    target_date = str(args.date).strip()

    if not out_path.is_file():
        raise SystemExit(f"Missing output_df: {out_path}")

    out = pd.read_csv(out_path, encoding="utf-8-sig")
    if "date" not in out.columns or "status" not in out.columns:
        raise SystemExit("output_df_new.csv needs columns: date, status (and url)")

    out["date"] = out["date"].astype(str).str.strip()
    out["status"] = out["status"].astype(str).str.strip()
    mask = (out["date"] == target_date) & (out["status"] == "download_failed")
    drop_df = out.loc[mask].copy()
    n_drop = len(drop_df)

    vids: set[str] = set()
    if "url" in drop_df.columns:
        for u in drop_df["url"].tolist():
            vid = _video_id_from_watch_url(str(u))
            if vid:
                vids.add(vid)

    print(f"data_root={data_root}")
    print(f"date={target_date} status=download_failed -> rows to remove from output_df: {n_drop}")
    print(f"video_ids to reset in crawl_yt_list (from URL): {len(vids)}")
    if n_drop and len(vids) < n_drop:
        print("(some rows may lack parsable watch URLs; queue reset uses IDs from URLs only)")

    if args.dry_run:
        return

    if n_drop == 0:
        print("Nothing to do.")
        return

    bak_out = out_path.with_suffix(out_path.suffix + ".bak")
    shutil.copy2(out_path, bak_out)
    print(f"Backed up output_df -> {bak_out}")

    out_kept = out.loc[~mask].copy()
    tmp = out_path.with_suffix(".csv.tmp")
    out_kept.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, out_path)
    print(f"Wrote output_df_new.csv ({len(out_kept)} rows)")

    if not queue_path.is_file():
        print(f"No crawl_yt_list.csv at {queue_path}; skip queue reset.")
        return

    shutil.copy2(queue_path, queue_path.with_suffix(queue_path.suffix + ".bak"))
    q = pd.read_csv(queue_path, encoding="utf-8-sig")
    if "video_id" not in q.columns:
        print("crawl_yt_list has no video_id; skip queue reset.")
        return

    q["video_id"] = q["video_id"].astype(str).str.strip()
    n_reset = 0
    for vid in vids:
        for idx in q.index[q["video_id"] == vid].tolist():
            q.at[idx, "status"] = "queued"
            q.at[idx, "retry_count"] = 0
            for col in ("last_error", "last_attempted_at", "done_at"):
                if col in q.columns:
                    q.at[idx, col] = ""
            n_reset += 1

    qt = queue_path.with_suffix(".csv.tmp")
    q.to_csv(qt, index=False, encoding="utf-8-sig")
    os.replace(qt, queue_path)
    print(f"Wrote crawl_yt_list.csv (reset {n_reset} queue row(s) across {len(vids)} video_id(s))")


if __name__ == "__main__":
    main()
