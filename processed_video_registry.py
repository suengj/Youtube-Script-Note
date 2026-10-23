"""Shared, append-only registry of YouTube videos already processed by P03."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REGISTRY_NAME = "processed_videos.csv"
REGISTRY_COLUMNS = ["video_id", "first_seen_at", "sources", "last_status", "model"]
REGISTRY_SOURCES = {"output_df", "sheet_marked", "manual_success", "channel_success", "skipped_historical"}


def registry_path(data_root: str | Path) -> Path:
    return Path(data_root) / REGISTRY_NAME


def read_registry(path: str | Path):
    p = Path(path)
    if not p.is_file():
        return None
    try:
        frame = pd.read_csv(p, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        frame = pd.read_csv(p, dtype=str, keep_default_na=False, encoding="cp949")
    for column in REGISTRY_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    return frame[REGISTRY_COLUMNS]


def write_registry(frame, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    os.close(fd)
    try:
        frame[REGISTRY_COLUMNS].to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def processed_ids(path: str | Path) -> set[str] | None:
    frame = read_registry(path)
    if frame is None:
        return None
    return {str(value).strip() for value in frame.video_id if str(value).strip() and str(value).strip().lower() != "unknown"}


def sheet_marked_ids(rows, normalize) -> set[str]:
    """Return video IDs with a non-empty Source or Download cell on any row."""
    if not rows:
        raise ValueError("Google Sheet returned no rows")
    headers = [str(value).strip().casefold() for value in rows[0]]
    url_i = next((i for i, value in enumerate(headers) if value == "url"), None)
    source_i = next((i for i, value in enumerate(headers) if value == "source"), None)
    download_i = next((i for i, value in enumerate(headers) if value == "download 여부"), None)
    if url_i is None or source_i is None or download_i is None:
        raise ValueError("Google Sheet is missing URL, Source, or Download 여부 header")
    ids = set()
    for row in rows[1:]:
        cells = list(row) + [""] * max(0, len(headers) - len(row))
        if not (str(cells[source_i] or "").strip() or str(cells[download_i] or "").strip()):
            continue
        item = normalize(str(cells[url_i] or ""))
        if item.get("kind") == "video":
            ids.add(item["video_id"])
    return ids


def read_sheet_marked_ids() -> set[str]:
    """Read live Sheet markers using the configured P03 spreadsheet connection."""
    from manual_video_input import (SHEET_ID, SHEET_TAB, build_sheets_service,
                                    normalize_youtube_url, read_sheet_rows)

    service = build_sheets_service()
    headers, values = read_sheet_rows(
        service,
        os.getenv("P03_GOOGLE_SHEET_ID", SHEET_ID),
        os.getenv("P03_GOOGLE_SHEET_TAB", SHEET_TAB),
    )
    rows = [headers, *values[1:]] if values else [headers]
    return sheet_marked_ids(rows, normalize_youtube_url)


def reconcile_sheet_marked(path: str | Path, sheet_ids) -> set[str]:
    """Record sheet-marked IDs missing from the registry and return all processed IDs."""
    current = processed_ids(path)
    if current is None:
        return set(sheet_ids)
    missing = set(sheet_ids) - current
    if missing:
        merge_records(path, [(vid, "sheet_marked", "sheet_marked", "") for vid in sorted(missing)])
    return current | set(sheet_ids)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def merge_records(path: str | Path, records) -> None:
    """Merge (video_id, source, status, model, first_seen_at) records atomically."""
    frame = read_registry(path)
    if frame is None:
        frame = pd.DataFrame(columns=REGISTRY_COLUMNS)
    by_id = {str(row.video_id).strip(): row.to_dict() for _, row in frame.iterrows() if str(row.video_id).strip()}
    now = _now()
    for record in records:
        video_id, source, status, model, *seen_at = record
        video_id = str(video_id or "").strip()
        source = str(source or "").strip()
        if not video_id or video_id.lower() == "unknown" or source not in REGISTRY_SOURCES:
            continue
        row = by_id.get(video_id, {"video_id": video_id, "first_seen_at": (seen_at[0] if seen_at else "") or now,
                                   "sources": "", "last_status": "", "model": ""})
        sources = set(filter(None, str(row.get("sources", "")).split(";")))
        sources.add(source)
        row["sources"] = ";".join(sorted(sources))
        if status not in (None, ""):
            row["last_status"] = str(status)
        if model not in (None, ""):
            row["model"] = str(model)
        if not row.get("first_seen_at"):
            row["first_seen_at"] = (seen_at[0] if seen_at else "") or now
        by_id[video_id] = row
    merged = pd.DataFrame([by_id[key] for key in sorted(by_id)], columns=REGISTRY_COLUMNS)
    write_registry(merged, path)


def record_success(data_root: str | Path, video_id: str, *, source: str, status: str = "success", model: str = "") -> None:
    merge_records(registry_path(data_root), [(video_id, source, status, model)])


def _video_id(value, normalize):
    raw = str(value or "").strip()
    if not raw or raw.lower() == "unknown":
        return ""
    item = normalize(raw)
    if item.get("kind") == "video":
        return item["video_id"]
    return raw if len(raw) == 11 and all(c.isalnum() or c in "_-" for c in raw) else ""


def build_registry(data_root: str | Path, *, include_sheet: bool = True, sheet_rows=None, dry_run: bool = False):
    """Build/merge all durable processed sources without deleting prior IDs."""
    from manual_video_input import normalize_youtube_url, read_ledger, build_sheets_service, read_sheet_rows, SHEET_ID, SHEET_TAB

    root = Path(data_root)
    records = []
    source_counts = {source: 0 for source in sorted(REGISTRY_SOURCES)}
    output_path = root / "output_df_new.csv"
    if output_path.is_file():
        try:
            output = pd.read_csv(output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        except UnicodeDecodeError:
            output = pd.read_csv(output_path, dtype=str, keep_default_na=False, encoding="cp949")
        for _, row in output.iterrows():
            vid = _video_id(row.get("v_id", ""), normalize_youtube_url)
            if not vid:
                for col in ("url", "video_url", "source_url"):
                    vid = _video_id(row.get(col, ""), normalize_youtube_url)
                    if vid:
                        break
            if vid:
                records.append((vid, "output_df", row.get("status", ""), row.get("model", "")))
                source_counts["output_df"] += 1
    ledger = read_ledger(root / "manual_input_status.csv")
    for _, row in ledger.iterrows():
        status = str(row.get("status", ""))
        if status not in {"success", "skipped_historical"}:
            continue
        vid = _video_id(row.get("video_id", ""), normalize_youtube_url)
        if vid:
            source = "manual_success" if status == "success" else "skipped_historical"
            records.append((vid, source, status, row.get("model", ""), row.get("first_seen_at", "")))
            source_counts[source] += 1
    if include_sheet:
        if sheet_rows is None:
            service = build_sheets_service()
            headers, values = read_sheet_rows(service, os.getenv("P03_GOOGLE_SHEET_ID", SHEET_ID),
                                              os.getenv("P03_GOOGLE_SHEET_TAB", SHEET_TAB))
            sheet_rows = [headers, *values[1:]] if values else [headers]
        if sheet_rows:
            headers = [str(v).strip() for v in sheet_rows[0]]
            url_i = next((i for i, v in enumerate(headers) if v.casefold() == "url"), None)
            source_i = next((i for i, v in enumerate(headers) if v.casefold() == "source"), 3)
            download_i = next((i for i, v in enumerate(headers) if v.casefold() == "download 여부"), 4)
            if url_i is not None:
                for row in sheet_rows[1:]:
                    values = list(row) + [""] * max(0, len(headers) - len(row))
                    if not (str(values[source_i] or "").strip() or str(values[download_i] or "").strip()):
                        continue
                    vid = _video_id(values[url_i], normalize_youtube_url)
                    if vid:
                        records.append((vid, "sheet_marked", "sheet_marked", ""))
                        source_counts["sheet_marked"] += 1
    if dry_run:
        return source_counts
    merge_records(registry_path(root), records)
    return source_counts
