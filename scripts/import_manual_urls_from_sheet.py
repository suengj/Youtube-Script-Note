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
                                build_sheets_service, read_sheet_rows, VIDEO_ID_RE)  # noqa: E402

def _read_sheet_rows(spreadsheet_id: str = SHEET_ID, tab: str = SHEET_TAB):
    service = build_sheets_service()
    return read_sheet_rows(service, spreadsheet_id, tab)[1]


def _date_ok(value: str, since: str | None) -> bool:
    if not since:
        return True
    s = str(value or "").strip()
    try:
        return date.fromisoformat(s[:10]).isoformat() == s[:10] and date.fromisoformat(since) <= date.fromisoformat(s[:10])
    except ValueError:
        return False


def _valid_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
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


def import_rows(rows, input_path: Path, data_root: Path, *, since: str | None = None,
                max_new: int | None = 5, dry_run: bool = False):
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
    download_i = col("Download 여부")
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
        records[-1]["downloaded"] = bool(str(row[download_i] or "").strip()) if download_i is not None else False
    input_df, encoding = _read_frame(input_path)
    if "url" not in input_df.columns:
        raise ValueError("input_df.csv must have a url column")
    existing_ids = {normalize_youtube_url(u)["video_id"] for u in input_df["url"] if normalize_youtube_url(u)["kind"] == "video"}
    out_path = data_root / "output_df_new.csv"
    out = _read_frame(out_path)[0] if out_path.exists() else pd.DataFrame(columns=["v_id", "url", "status"])
    ledger = read_ledger(data_root / "manual_input_status.csv")
    ledger_ids = set(ledger.video_id.astype(str))
    ledger_status = dict(zip(ledger.video_id.astype(str), ledger.status.astype(str)))
    output_ids = set()
    output_success = set()
    for _, row in out.iterrows():
        row_ids = set()
        for name in ("v_id", "video_id", "id"):
            if name in out.columns and str(row.get(name, "")).strip():
                value = str(row[name]).strip()
                candidate = normalize_youtube_url(value)
                if candidate["kind"] == "video":
                    row_ids.add(candidate["video_id"])
                elif VIDEO_ID_RE.fullmatch(value):
                    row_ids.add(value)
        for name in ("url", "video_url", "source_url"):
            if name in out.columns and str(row.get(name, "")).strip():
                candidate = normalize_youtube_url(str(row[name]).strip())
                if candidate["kind"] == "video":
                    row_ids.add(candidate["video_id"])
        if row_ids:
            output_ids.update(row_ids)
            if str(row.get("status", "")).strip().lower() == "success":
                output_success.update(row_ids)
    sheet_downloaded_ids = {
        rec["norm"]["video_id"] for rec in records
        if rec["norm"]["kind"] == "video" and rec["downloaded"]
    }
    new = []
    seen = set()
    already_success = already_queued = already_processed = 0
    for rec in records:
        n = rec["norm"]
        if n["kind"] != "video":
            continue
        vid = n["video_id"]
        if rec["downloaded"] or vid in sheet_downloaded_ids:
            already_processed += 1
        elif vid in output_ids or vid in ledger_ids or vid in existing_ids or vid in seen:
            already_processed += 1
            if vid in output_success or ledger_status.get(vid) == "success":
                already_success += 1
            else:
                already_queued += 1
        else:
            seen.add(vid)
            new.append(rec)
    # Newest eligible rows get the limited slots first.
    new.sort(key=lambda rec: rec["date"] or "", reverse=True)
    eligible_count = len(new)
    if max_new is not None:
        new = new[:max(0, int(max_new))]
    deferred = eligible_count - len(new)
    print(f"total rows: {len(records)}")
    print(f"video: {counts['video']}")
    print(f"channel: {counts['channel']}")
    print(f"playlist: {counts['playlist']}")
    print(f"unsupported: {counts['unsupported']}")
    print(f"already-success: {already_success}")
    print(f"already-queued: {already_queued}")
    print(f"already-processed: {already_processed}")
    print(f"new: {len(new)}")
    print(f"deferred: {deferred}")
    if dry_run:
        return {"new": len(new), "deferred": deferred, "already_processed": already_processed, **counts, "total": len(records)}
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
    return {"new": len(new), "deferred": deferred, "already_processed": already_processed, **counts, "total": len(records)}


def import_from_sheet(data_root: str | Path, *, spreadsheet_id: str | None = None,
                      tab: str | None = None, since: str | None = None,
                      dry_run: bool = False):
    """Fetch the configured Sheet and append unseen video IDs under data_root."""
    root = Path(data_root)
    if max_new := os.getenv("P03_SHEET_IMPORT_MAX"):
        try:
            max_new = int(max_new)
            if max_new < 0:
                raise ValueError
        except ValueError:
            raise ValueError("P03_SHEET_IMPORT_MAX must be a non-negative integer")
    else:
        max_new = 5
    rows = _read_sheet_rows(
        spreadsheet_id or os.getenv("P03_GOOGLE_SHEET_ID", SHEET_ID),
        tab or os.getenv("P03_GOOGLE_SHEET_TAB", SHEET_TAB),
    )
    return import_rows(rows, root / "input_df.csv", root, since=since, max_new=max_new, dry_run=dry_run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path(os.getenv("DATA_ROOT", ROOT / "data")))
    parser.add_argument("--spreadsheet-id", default=os.getenv("P03_GOOGLE_SHEET_ID", SHEET_ID))
    parser.add_argument("--tab", default=os.getenv("P03_GOOGLE_SHEET_TAB", SHEET_TAB))
    parser.add_argument("--since", help="include rows dated YYYY-MM-DD or later")
    parser.add_argument("--all", action="store_true", help="explicitly allow importing without a date cutoff")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.since and not _valid_date(args.since):
        parser.error("--since must be a valid YYYY-MM-DD date")
    if not args.since and not args.all:
        parser.error("--since is required unless --all is explicitly set")
    try:
        import_from_sheet(args.data_root, spreadsheet_id=args.spreadsheet_id,
                          tab=args.tab, since=args.since, dry_run=args.dry_run)
    except Exception as exc:
        print(f"Google Sheet import aborted: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    main()
