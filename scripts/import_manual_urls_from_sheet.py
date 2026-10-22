#!/usr/bin/env python3
"""Read the owner's YT Link sheet and append unseen video URLs locally."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from manual_video_input import (SHEET_ID, SHEET_TAB, normalize_youtube_url, read_ledger,
                                build_sheets_service, read_sheet_rows)  # noqa: E402

def _read_sheet_rows(spreadsheet_id: str = SHEET_ID, tab: str = SHEET_TAB):
    service = build_sheets_service()
    return read_sheet_rows(service, spreadsheet_id, tab)[1]


def _date_ok(value: str, since: str | None) -> bool:
    if not since:
        return True
    s = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(s) >= date.fromisoformat(since)
    except ValueError:
        return False


def _read_frame(path: Path):
    import pandas as pd
    if not path.exists():
        return pd.DataFrame(columns=["date", "url"]), "cp949"
    try:
        return pd.read_csv(path, encoding="cp949", dtype=str, keep_default_na=False), "cp949"
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False), "utf-8-sig"


def _write_frame_atomic(frame, path: Path, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        frame.to_csv(tmp, index=False, encoding=encoding)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def import_rows(rows, input_path: Path, data_root: Path, *, since: str | None = None, dry_run: bool = False):
    import pandas as pd
    header_idx = 0 if rows and any(str(c).strip().casefold() == "url" for c in rows[0]) else None
    if header_idx is None:
        raise ValueError("Google Sheet row 1 must contain headers")
    headers = [str(h).strip() for h in rows[header_idx]]
    missing = [h for h in ("URL", "Source", "Download 여부") if h not in headers]
    if missing:
        raise ValueError("Google Sheet row 1 is missing required header(s): " + ", ".join(missing))
    url_i = next(i for i, h in enumerate(headers) if h.casefold() == "url")
    def col(name):
        return next((i for i, h in enumerate(headers) if h.casefold() == name.casefold()), None)
    date_i, title_i, source_i = col("Updated/Published Time/"), col("Title"), col("Source")
    records = []
    counts = {"video": 0, "channel": 0, "playlist": 0, "unsupported": 0}
    for row in rows[header_idx + 1:]:
        row = list(row) + [""] * max(0, len(headers) - len(row))
        if not _date_ok(row[date_i] if date_i is not None else "", since):
            continue
        url = str(row[url_i] or "").strip()
        if not url:
            continue
        norm = normalize_youtube_url(url)
        counts[norm["kind"] if norm["kind"] in counts else "unsupported"] += 1
        records.append({"url": url, "norm": norm,
                        "date": str(row[date_i]).strip() if date_i is not None else "",
                        "title": str(row[title_i]).strip() if title_i is not None else "",
                        "source": str(row[source_i]).strip() if source_i is not None else ""})
    input_df, encoding = _read_frame(input_path)
    if "url" not in input_df.columns:
        raise ValueError("input_df.csv must have a url column")
    existing_ids = {normalize_youtube_url(u)["video_id"] for u in input_df["url"] if normalize_youtube_url(u)["kind"] == "video"}
    out_path = data_root / "output_df_new.csv"
    out = _read_frame(out_path)[0] if out_path.exists() else pd.DataFrame(columns=["v_id", "url", "status"])
    success = set(out.loc[out.get("status", pd.Series(dtype=str)).astype(str).str.lower() == "success", "v_id"].astype(str)) if "v_id" in out.columns and "status" in out.columns else set()
    queued = existing_ids | (set(out.get("v_id", pd.Series(dtype=str)).astype(str)) - success)
    ledger = read_ledger(data_root / "manual_input_status.csv")
    ledger_success = set(ledger.loc[ledger.status == "success", "video_id"].astype(str))
    new = []
    seen = set()
    already_success = already_queued = 0
    for rec in records:
        n = rec["norm"]
        if n["kind"] != "video":
            continue
        vid = n["video_id"]
        if vid in success or vid in ledger_success:
            already_success += 1
        elif vid in queued or vid in seen:
            already_queued += 1
        else:
            seen.add(vid)
            new.append(rec)
    print(f"total rows: {len(records)}")
    print(f"video: {counts['video']}")
    print(f"channel: {counts['channel']}")
    print(f"playlist: {counts['playlist']}")
    print(f"unsupported: {counts['unsupported']}")
    print(f"already-success: {already_success}")
    print(f"already-queued: {already_queued}")
    print(f"new: {len(new)}")
    if dry_run:
        return {"new": len(new), **counts, "total": len(records)}
    for rec in new:
        row = {c: "" for c in input_df.columns}
        row["url"] = rec["url"]
        if "date" in row:
            row["date"] = rec["date"]
        for c, val in (("title", rec["title"]), ("source", rec["source"])):
            if c in row:
                row[c] = val
        input_df.loc[len(input_df)] = row
    _write_frame_atomic(input_df, input_path, encoding)
    return {"new": len(new), **counts, "total": len(records)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path(os.getenv("DATA_ROOT", ROOT / "data")))
    parser.add_argument("--spreadsheet-id", default=os.getenv("P03_GOOGLE_SHEET_ID", SHEET_ID))
    parser.add_argument("--tab", default=os.getenv("P03_GOOGLE_SHEET_TAB", SHEET_TAB))
    parser.add_argument("--since", help="include rows dated YYYY-MM-DD or later")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        rows = _read_sheet_rows(args.spreadsheet_id, args.tab)
    except Exception as exc:
        print(f"Google Sheet import aborted: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    import_rows(rows, args.data_root / "input_df.csv", args.data_root, since=args.since, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    main()
