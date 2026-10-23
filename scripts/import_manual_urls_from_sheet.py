#!/usr/bin/env python3
"""Read the owner's YT Link sheet and append unseen video URLs locally."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from manual_video_input import (SHEET_ID, SHEET_TAB, normalize_youtube_url,
                                build_sheets_service, read_sheet_rows)  # noqa: E402
from input_df_csv import read_input_df  # noqa: E402
from processed_video_registry import (input_video_ids, processed_ids, processed_id_union,
                                      read_registry)  # noqa: E402

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
    return read_input_df(path, dtype=str, keep_default_na=False, return_encoding=True)


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
    registry_path = Path(data_root) / "processed_videos.csv"
    registry = processed_ids(registry_path)
    if registry is None:
        logging.getLogger(__name__).warning("processed_videos.csv is missing; sheet import is fail-closed")
        return {"new": 0, "deferred": 0, "already_processed": 0,
                "video": 0, "channel": 0, "playlist": 0, "unsupported": 0, "total": 0}
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
    output_path = Path(data_root) / "output_df_new.csv"
    if output_path.is_file():
        try:
            output_df = pd.read_csv(output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        except UnicodeDecodeError:
            output_df = pd.read_csv(output_path, dtype=str, keep_default_na=False, encoding="cp949")
    else:
        output_df = pd.DataFrame()
    try:
        all_processed = processed_id_union(
            data_root, normalize_youtube_url, output_df,
            sheet_rows=[rows[header_idx], *rows[header_idx + 1:]], input_df=input_df)
    except Exception as exc:
        logging.getLogger(__name__).warning("Processed-state read/validation failed; refusing new imports: %s", exc)
        return {"new": 0, "deferred": 0, "already_processed": 0,
                "video": 0, "channel": 0, "playlist": 0, "unsupported": 0, "total": 0}
    input_ids = input_video_ids(input_df, normalize_youtube_url)
    new = []
    seen = set()
    already_success = already_queued = already_processed = 0
    registry_frame = read_registry(Path(data_root) / "processed_videos.csv")
    registry_rows = {str(row.video_id): row for _, row in registry_frame.iterrows()}
    for rec in records:
        n = rec["norm"]
        if n["kind"] != "video":
            continue
        vid = n["video_id"]
        if vid in input_ids:
            already_queued += 1
        elif vid in all_processed or vid in seen:
            already_processed += 1
            registry_row = registry_rows.get(vid)
            if registry_row is not None and str(registry_row.last_status).strip().lower() in {"success", "manual_success", "channel_success"}:
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
    # Exclude all processed and already queued IDs before applying the cap so
    # a newer existing row cannot starve an older unseen row across runs.
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
    if new and "manual_imported" not in input_df.columns:
        input_df["manual_imported"] = ""
    for rec in new:
        row = {c: "" for c in input_df.columns}
        row["url"] = rec["url"]
        row["manual_imported"] = "true"
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
    if processed_ids(root / "processed_videos.csv") is None:
        logging.getLogger(__name__).warning("processed_videos.csv is missing; sheet import is fail-closed")
        return {"new": 0, "deferred": 0, "already_processed": 0,
                "video": 0, "channel": 0, "playlist": 0, "unsupported": 0, "total": 0}
    if max_new := os.getenv("P03_SHEET_IMPORT_MAX"):
        try:
            max_new = int(max_new)
            if max_new < 0:
                raise ValueError
        except ValueError:
            raise ValueError("P03_SHEET_IMPORT_MAX must be a non-negative integer")
    else:
        max_new = 5
    try:
        rows = _read_sheet_rows(
            spreadsheet_id or os.getenv("P03_GOOGLE_SHEET_ID", SHEET_ID),
            tab or os.getenv("P03_GOOGLE_SHEET_TAB", SHEET_TAB),
        )
    except Exception as exc:
        # A failed Sheet read cannot establish which new rows are safe to import.
        logging.getLogger(__name__).warning(
            "Google Sheet read failed; refusing new imports: %s", exc)
        return {"new": 0, "deferred": 0, "already_processed": 0,
                "video": 0, "channel": 0, "playlist": 0, "unsupported": 0, "total": 0}
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
