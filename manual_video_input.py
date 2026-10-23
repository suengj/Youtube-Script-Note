"""Normalization and local status ledger for owner supplied YouTube URLs."""
from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from input_df_csv import normalize_input_df_columns

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LEDGER_COLUMNS = ["video_id", "canonical_url", "source_urls", "first_seen_at", "status", "reason", "attempts", "regen_attempts", "last_attempt_at", "done_at", "md_path", "md_sha256", "drive_verified", "model", "sheet_synced", "needs_manual_check"]
LEDGER_STATUSES = {"new", "processing", "success", "retryable_failed", "pending_quarantine", "unsupported", "skipped_policy", "skipped_historical"}
MAX_REGEN_ATTEMPTS = 2
SHEET_ID = "1iRtWdbbJGM8EY1K0jDXkt36IImQM-BmhDoufsFOjRKI"
SHEET_TAB = "YT Link"
SHEET_HEADERS = ("URL", "Source", "Download 여부")
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
IMPORTER_MARKER_COLUMN = "manual_imported"
HISTORICAL_SKIP_REASON = "historical input_df row before manual cutoff; owner: already processed"


def parse_input_date(value) -> date | None:
    """Parse input_df dates in the legacy dotted or ISO form."""
    raw = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})[.-](\d{1,2})[.-](\d{1,2})", raw)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def select_manual_input_rows(input_df, cutoff: str | None, max_rows: int | None = 5, *, exclude_ids=None):
    """Return eligible rows in newest-first order, plus all eligible video IDs.

    When cutoff is absent or invalid, only rows explicitly marked by the sheet
    importer are eligible. The caller owns warning logging for that fallback.
    """
    import pandas as pd

    valid_cutoff = None
    try:
        if cutoff:
            valid_cutoff = date.fromisoformat(str(cutoff).strip())
            if valid_cutoff.isoformat() != str(cutoff).strip():
                valid_cutoff = None
    except ValueError:
        pass
    has_cutoff = valid_cutoff is not None
    eligible = []
    eligible_ids = set()
    if input_df is None or "url" not in input_df.columns:
        return [], eligible_ids
    for ordinal, (_, row) in enumerate(input_df.iterrows()):
        url = str(row.get("url", "") or "").strip()
        if not url:
            continue
        if has_cutoff:
            row_date = parse_input_date(row.get("date", ""))
            if row_date is None or row_date < valid_cutoff:
                continue
            rank_date = row_date
        else:
            marker = str(row.get(IMPORTER_MARKER_COLUMN, "") or "").strip().lower()
            if marker not in {"1", "true", "yes", "y"}:
                continue
            rank_date = parse_input_date(row.get("date", "")) or date.min
        item = normalize_youtube_url(url)
        if item["kind"] == "video":
            eligible_ids.add(item["video_id"])
        eligible.append((rank_date, item.get("video_id") or url, ordinal, url))
    # Newest date first. Stable tie breakers make queue membership reproducible.
    eligible.sort(key=lambda item: (-item[0].toordinal(), item[1], item[3], item[2]))
    unique = []
    seen = set()
    for item in eligible:
        identity = item[1]
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(item)
    excluded = {str(value).strip() for value in (exclude_ids or ()) if str(value).strip()}
    candidates = [item[3] for item in unique
                  if normalize_youtube_url(item[3]).get("video_id") not in excluded]
    if max_rows is not None:
        candidates = candidates[:max(0, int(max_rows))]
    return candidates, eligible_ids


def terminal_manual_ids(ledger, max_retries: int = 3) -> set[str]:
    """IDs already terminal or intentionally held from a new manual attempt."""
    terminal_statuses = {"success", "skipped_historical", "unsupported", "skipped_policy", "pending_quarantine"}
    result = set()
    if ledger is None or not len(ledger):
        return result
    for _, row in ledger.iterrows():
        vid = str(row.get("video_id", "")).strip()
        if not vid:
            continue
        status = str(row.get("status", ""))
        reason = str(row.get("reason", "") or "")
        attempts = _attempt_count(row.get("attempts"))
        regen_attempts = _attempt_count(row.get("regen_attempts"))
        regen_pending = ("staged Markdown missing or hash mismatch" in reason or
                         reason.startswith("regen attempt") or regen_attempts > 0)
        if (status in terminal_statuses or
                status == "retryable_failed" and (
                    reason.startswith("pending_drive:") or reason == "regen cap reached" or
                    regen_pending and regen_attempts >= MAX_REGEN_ATTEMPTS or
                    not regen_pending and attempts >= max_retries)):
            result.add(vid)
    return result


def skip_historical_processing(ledger, input_df, cutoff: str | None):
    """Permanently skip processing ledger rows whose input date predates cutoff."""
    try:
        cutoff_date = date.fromisoformat(str(cutoff).strip()) if cutoff else None
        if cutoff_date and cutoff_date.isoformat() != str(cutoff).strip():
            cutoff_date = None
    except ValueError:
        cutoff_date = None
    input_rows = {}
    if input_df is not None:
        normalize_input_df_columns(input_df)
    if input_df is not None and "url" in input_df.columns:
        for _, input_row in input_df.iterrows():
            value = input_row.get("url", "")
            item = normalize_youtube_url(value)
            if item["kind"] == "video":
                input_rows[item["video_id"]] = input_row
    for idx, row in ledger.iterrows():
        status = str(row.get("status", ""))
        if status == "deferred_backfill":
            ledger.at[idx, "status"] = "skipped_historical"
            ledger.at[idx, "reason"] = HISTORICAL_SKIP_REASON
            continue
        if status != "processing":
            continue
        vid = str(row.get("video_id", ""))
        input_row = input_rows.get(vid)
        row_date = parse_input_date(input_row.get("date", "")) if input_row is not None else None
        if cutoff_date and row_date and row_date < cutoff_date:
            ledger.at[idx, "status"] = "skipped_historical"
            ledger.at[idx, "reason"] = HISTORICAL_SKIP_REASON
    return ledger


def exclude_skipped_historical_urls(urls, ledger):
    """Remove terminal historical IDs from any queue, including channel candidates."""
    historical_ids = set(ledger.loc[
        ledger.status.astype(str) == "skipped_historical", "video_id"
    ].astype(str)) if len(ledger) else set()
    return [url for url in urls
            if normalize_youtube_url(url).get("video_id") not in historical_ids]


def normalize_youtube_url(value: str) -> dict:
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw if "://" in raw else "https://" + raw)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host not in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}:
            return {"kind": "unsupported", "video_id": "", "canonical_url": "", "reason": "not a supported YouTube URL"}
        parts = [p for p in parsed.path.split("/") if p]
        vid = ""
        if host == "youtu.be":
            vid = parts[0] if parts else ""
        elif parsed.path.rstrip("/") == "/watch":
            vid = (parse_qs(parsed.query).get("v") or [""])[0]
            if not vid and parse_qs(parsed.query).get("list"):
                return {"kind": "playlist", "video_id": "", "canonical_url": "", "reason": "playlist URL has no video ID"}
        elif len(parts) >= 2 and parts[0] in {"shorts", "live", "embed"}:
            vid = parts[1]
        elif parts and (parts[0].startswith("@") or parts[0] in {"channel", "c", "user"}):
            return {"kind": "channel", "video_id": "", "canonical_url": "", "reason": "channel URL is not an individual video"}
        elif parse_qs(parsed.query).get("list"):
            return {"kind": "playlist", "video_id": "", "canonical_url": "", "reason": "playlist URL is not an individual video"}
        else:
            return {"kind": "unsupported", "video_id": "", "canonical_url": "", "reason": "unsupported YouTube URL shape"}
        if not VIDEO_ID_RE.fullmatch(vid):
            return {"kind": "unsupported", "video_id": "", "canonical_url": "", "reason": "missing or invalid 11-character video ID"}
        return {"kind": "video", "video_id": vid, "canonical_url": f"https://www.youtube.com/watch?v={vid}", "reason": ""}
    except (TypeError, ValueError):
        return {"kind": "unsupported", "video_id": "", "canonical_url": "", "reason": "invalid URL"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _attempt_count(value) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _ensure_ledger_flag_columns(df):
    """Keep persisted boolean-like ledger flags assignable as strings."""
    for col in ("drive_verified", "sheet_synced", "needs_manual_check"):
        if col not in df.columns:
            df[col] = ""
        else:
            # Callers may construct a ledger directly, leaving an all-empty
            # flag column inferred as float64 under pandas 3.
            df[col] = df[col].astype(object)
    return df


def read_ledger(path: str | Path, *, require_schema: bool = False):
    import pandas as pd
    p = Path(path)
    if not p.exists():
        return _ensure_ledger_flag_columns(pd.DataFrame(columns=LEDGER_COLUMNS))
    try:
        df = pd.read_csv(p, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(p, dtype=str, keep_default_na=False, encoding="cp949")
    missing = sorted(set(LEDGER_COLUMNS) - set(df.columns))
    if require_schema and missing:
        raise ValueError(f"manual ledger missing required columns: {', '.join(missing)}")
    for col in missing:
        df[col] = ""
    _ensure_ledger_flag_columns(df)
    # Counters remain numeric in memory; all other ledger CSV values stay text.
    for col in ("attempts", "regen_attempts"):
        df[col] = df[col].map(_attempt_count)
    # Migrate the earlier, non-terminal name without making those rows runnable.
    legacy = df.status.astype(str) == "deferred_backfill"
    df.loc[legacy, "status"] = "skipped_historical"
    df.loc[legacy, "reason"] = HISTORICAL_SKIP_REASON
    return df[LEDGER_COLUMNS]


def save_ledger(df, path: str | Path) -> None:
    """Atomic replace, matching output_df's single-writer persistence model."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    os.close(fd)
    try:
        df[LEDGER_COLUMNS].to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def upsert_ledger(df, *, video_id: str, canonical_url: str = "", source_url: str = "", status: str,
                  reason: str = "", md_path: str = "", drive_verified: str = "", model: str = "",
                  sheet_synced: str = "", md_sha256: str = "", attempted: bool = False):
    if status not in LEDGER_STATUSES:
        raise ValueError(f"invalid manual ledger status: {status}")
    _ensure_ledger_flag_columns(df)
    now = _now()
    matches = df.index[df.video_id.astype(str) == str(video_id)].tolist() if len(df) else []
    if matches:
        i = matches[-1]
        row = df.loc[i].to_dict()
        sources = [x for x in str(row.get("source_urls", "")).split(" | ") if x]
        if source_url and source_url not in sources:
            sources.append(source_url)
        row.update({"canonical_url": canonical_url or row.get("canonical_url", ""), "source_urls": " | ".join(sources),
                    "status": status, "reason": reason, "md_path": md_path or row.get("md_path", ""),
                    "md_sha256": md_sha256 or row.get("md_sha256", ""),
                    "drive_verified": drive_verified or row.get("drive_verified", ""),
                    "model": model or row.get("model", ""),
                    "sheet_synced": sheet_synced if sheet_synced != "" else row.get("sheet_synced", "false")})
        row["attempts"] = _attempt_count(row.get("attempts")) + (1 if attempted else 0)
        if attempted:
            row["last_attempt_at"] = now
        if status == "success":
            row["done_at"] = now
        df.loc[i] = row
    else:
        df.loc[len(df)] = {"video_id": video_id, "canonical_url": canonical_url,
                           "source_urls": source_url, "first_seen_at": now, "status": status,
                           "reason": reason, "attempts": 1 if attempted else 0,
                           "last_attempt_at": now if attempted else "", "done_at": now if status == "success" else "",
                           "md_path": md_path, "md_sha256": md_sha256, "drive_verified": drive_verified, "model": model,
                           "sheet_synced": sheet_synced or "false"}
    return df


def prepare_manual_queue(manual_rows, channel_urls, output_df, ledger, max_retries: int = 3,
                         *, duration_map=None, shorts_threshold_seconds: int = 0,
                         channel_auto_sub_only=None, video_channel_ids=None,
                         processed_video_ids=None):
    """Return unique canonical manual items and the ledger with pre-run decisions recorded."""
    if processed_video_ids is None:
        logging.getLogger(__name__).warning("processed_videos.csv is unavailable; manual queue is fail-closed")
        return [], ledger
    channel_ids = set()
    for url in channel_urls:
        item = normalize_youtube_url(url)
        if item["kind"] == "video":
            channel_ids.add(item["video_id"])
    processed_video_ids = set(processed_video_ids)
    # Rebuild the owner-defined admission union here as well as in main so
    # alternate callers cannot admit an ID just because the registry is stale.
    import processed_video_registry
    processed_video_ids |= processed_video_registry.output_processed_ids(
        output_df, normalize_youtube_url)
    if len(ledger):
        processed_video_ids |= set(ledger.loc[
            ledger.status.astype(str).isin(["success", "skipped_historical"]), "video_id"
        ].astype(str))
    queued, seen = [], set()
    for source_url in manual_rows:
        source_url = str(source_url or "").strip()
        if not source_url:
            continue
        item = normalize_youtube_url(source_url)
        if item["kind"] != "video":
            key = "unsupported:" + hashlib.sha1(source_url.encode()).hexdigest()[:12]
            ledger = upsert_ledger(ledger, video_id=key, source_url=source_url,
                                   status="unsupported", reason=item["reason"])
            continue
        vid = item["video_id"]
        if vid in processed_video_ids:
            continue
        prior = ledger[ledger.video_id.astype(str) == vid]
        prior_status = str(prior.iloc[-1].get("status") or "") if len(prior) else ""
        # Successful and historical rows are terminal owner records. Never
        # rewrite them, even when a caller's registry snapshot is stale.
        if prior_status in {"success", "skipped_historical"}:
            continue
        if prior_status == "unsupported":
            ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                   source_url=source_url, status="unsupported",
                                   reason=str(prior.iloc[-1].get("reason") or "previously classified unsupported"))
            continue
        duration_iso = (duration_map or {}).get(vid, "")
        if shorts_threshold_seconds > 0 and duration_iso:
            from channel_crawl import _parse_duration_to_seconds
            seconds = _parse_duration_to_seconds(str(duration_iso))
            if seconds is not None and seconds <= shorts_threshold_seconds:
                ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                       source_url=source_url, status="skipped_policy",
                                       reason=f"Shorts duration policy: {seconds}s <= {shorts_threshold_seconds}s")
                continue
        if prior_status == "pending_quarantine":
            continue
        pending_drive = (prior_status == "retryable_failed" and
                         str(prior.iloc[-1].get("reason") or "").startswith("pending_drive:"))
        if pending_drive:
            continue
        attempts = _attempt_count(prior.iloc[-1].get("attempts")) if len(prior) else 0
        reason = str(prior.iloc[-1].get("reason") or "") if len(prior) else ""
        regen_attempts = _attempt_count(prior.iloc[-1].get("regen_attempts")) if len(prior) else 0
        if prior_status == "retryable_failed" and reason == "regen cap reached":
            continue
        regen_pending = ("staged Markdown missing or hash mismatch" in reason or
                         reason.startswith("regen attempt") or regen_attempts > 0)
        # Apply both recovery caps before channel overlap can merge this item into
        # the channel queue and change its ledger state to processing.
        if prior_status == "retryable_failed" and regen_pending and regen_attempts >= MAX_REGEN_ATTEMPTS:
            ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                   source_url=source_url, status="retryable_failed", reason="regen cap reached")
            continue
        if prior_status == "retryable_failed" and not regen_pending and attempts >= max_retries:
            ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                   source_url=source_url, status="retryable_failed",
                                   reason=f"retry limit reached ({max_retries})")
            continue
        if vid in channel_ids:
            # Preserve manual ownership so channel processing can also finalize the
            # manual ledger. The caller merges this ID with its channel URL and only
            # processes it once; do not overwrite an in-flight Drive retry reason.
            if prior_status == "retryable_failed" and str(prior.iloc[-1].get("reason") or "").startswith("pending_drive:"):
                continue
            if vid not in seen:
                seen.add(vid)
                queued.append((vid, item["canonical_url"], source_url))
                if prior_status == "retryable_failed" and regen_pending:
                    mask = ledger.video_id.astype(str) == vid
                    ledger.loc[mask, "regen_attempts"] = regen_attempts + 1
                    ledger.loc[mask, "last_attempt_at"] = _now()
                    ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                           source_url=source_url, status="processing",
                                           reason=f"regen attempt {regen_attempts + 1}/{MAX_REGEN_ATTEMPTS}")
                else:
                    ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                           source_url=source_url, status="processing",
                                           reason="manual ownership retained; selected channel candidate will process once",
                                           attempted=True)
            continue
        if prior_status == "retryable_failed" and regen_pending:
            if vid not in seen:
                seen.add(vid)
                queued.append((vid, item["canonical_url"], source_url))
                mask = ledger.video_id.astype(str) == vid
                ledger.loc[mask, "regen_attempts"] = regen_attempts + 1
                ledger.loc[mask, "status"] = "processing"
                ledger.loc[mask, "reason"] = f"regen attempt {regen_attempts + 1}/{MAX_REGEN_ATTEMPTS}"
            continue
        if vid in seen:
            ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                   source_url=source_url, status="processing",
                                   reason="duplicate URL form merged by video ID")
            continue
        seen.add(vid)
        queued.append((vid, item["canonical_url"], source_url))
        ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                               source_url=source_url, status="processing", attempted=True)
    return queued, ledger


def ledger_outcome(status: str, error: str = "", md_path: str = "", *,
                   md_exists: bool = False, drive_disabled: bool = False, drive_verified: bool = False):
    """Map the pipeline result to the stricter manual ledger completion rule."""
    policy_statuses = {"live_scheduled", "skipped_auto_subs_only", "passed_shorts", "oversized_file"}
    if status == "success":
        if not md_exists:
            return "retryable_failed", "pipeline reported success but final Markdown path is missing", md_path, "false"
        if not drive_verified:
            return "retryable_failed", "pending_drive: final Markdown is staged but Drive verification is pending", md_path, "false"
        return "success", "", md_path, "verified"
    if status in policy_statuses:
        return "skipped_policy", f"pipeline policy/status: {status}", md_path, "false"
    if status == "video_unavailable":
        return "unsupported", error or "video unavailable/private", md_path, "false"
    return "retryable_failed", error or status, md_path, "false"


def build_sheets_service(key_file: str | None = None):
    """Load credentials without exposing key contents and build the Sheets client."""
    key_file = (key_file or os.getenv("P03_GOOGLE_SA_JSON", "")).strip()
    if not key_file or not Path(key_file).is_file():
        raise FileNotFoundError("Google service account key file is missing (P03_GOOGLE_SA_JSON)")
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    credentials = Credentials.from_service_account_file(key_file, scopes=[SHEETS_SCOPE])
    import logging
    logging.getLogger(__name__).info("Google Sheets service account: %s", credentials.service_account_email)
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _quoted_tab(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def validate_sheet(service, spreadsheet_id: str = SHEET_ID, tab: str = SHEET_TAB):
    """Verify tab and required row-1 headers before reading or writing sheet values."""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title").execute()
    tabs = {s.get("properties", {}).get("title", "") for s in meta.get("sheets", [])}
    if tab not in tabs:
        raise ValueError(f"Google Sheet tab not found: {tab}")
    response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{_quoted_tab(tab)}!1:1").execute()
    headers = [str(v).strip() for v in (response.get("values") or [[]])[0]] if response.get("values") else []
    missing = [name for name in SHEET_HEADERS if name not in headers]
    if missing:
        raise ValueError("Google Sheet row 1 is missing required header(s): " + ", ".join(missing))
    return headers


def read_sheet_rows(service, spreadsheet_id: str = SHEET_ID, tab: str = SHEET_TAB):
    headers = validate_sheet(service, spreadsheet_id, tab)
    values = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{_quoted_tab(tab)}!A:Z").execute().get("values", [])
    return headers, values


def writeback_success_rows(ledger, service, *, spreadsheet_id: str = SHEET_ID, tab: str = SHEET_TAB):
    """Write model and blank provenance cells for successful, Drive-verified manual videos."""
    _ensure_ledger_flag_columns(ledger)
    def col_letter(n):
        result = ""
        while n:
            n, rem = divmod(n - 1, 26)
            result = chr(65 + rem) + result
        return result
    wanted = ledger[(ledger.status == "success") &
                    (ledger.drive_verified.astype(str) == "verified") &
                    (ledger.sheet_synced.astype(str).str.lower() != "true") &
                    (ledger.needs_manual_check.astype(str).str.lower() != "true")]
    def read_columns(columns):
        ranges = [f"{_quoted_tab(tab)}!{col_letter(col)}2:{col_letter(col)}" for col in columns]
        response = service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id, ranges=ranges).execute()
        return [entry.get("values", []) for entry in response.get("valueRanges", [])]

    def map_rows(columns):
        urls, sources, downloads = columns
        rows = []
        for offset in range(max(len(urls), len(sources), len(downloads))):
            url_cell = urls[offset] if offset < len(urls) else []
            url_text = str((url_cell or [""])[0] or "")
            normalized = normalize_youtube_url(url_text)
            video_id = normalized["video_id"] if normalized["kind"] == "video" else ""
            source = str((sources[offset] or [""])[0] or "") if offset < len(sources) else ""
            download = str((downloads[offset] or [""])[0] or "") if offset < len(downloads) else ""
            rows.append({"row": offset + 2, "url": url_text, "video_id": video_id,
                         "source": source, "download": download})
        return rows

    def row_sequence(rows):
        return [(row["row"], row["url"], row["video_id"]) for row in rows]

    def by_video_id(rows):
        result = {}
        for row in rows:
            if row["video_id"]:
                result.setdefault(row["video_id"], []).append(row)
        return result

    def updates_for(row, row_number=None):
        row_number = row["row"] if row_number is None else row_number
        return [
            {"range": f"{quoted}!{col_letter(source_col)}{row_number}", "values": [[row["source"]]]},
            {"range": f"{quoted}!{col_letter(model_col)}{row_number}", "values": [[row["download"]]]},
        ]

    for _, item in wanted.iterrows():
        vid = str(item.video_id)
        model = str(item.get("model", "") or "").strip()
        if not model:
            continue
        headers = validate_sheet(service, spreadsheet_id, tab)
        columns = {name: headers.index(name) + 1 for name in SHEET_HEADERS}
        url_col, source_col, model_col = (columns[k] for k in SHEET_HEADERS)
        quoted = _quoted_tab(tab)
        try:
            snapshot = map_rows(read_columns((url_col, source_col, model_col)))
            targets_before = [row for row in snapshot if row["video_id"] == vid]
            if targets_before:
                if all((row["source"] if row["source"].strip() else "own") == row["source"]
                       and row["download"] == model for row in targets_before):
                    # The sheet already contains the requested state. No API write
                    # means there is no stale-row race to reconcile.
                    ledger.loc[ledger.video_id.astype(str) == vid, "sheet_synced"] = "true"
                    continue
        except Exception:
            # The pre-write snapshot failed before any mutation was requested.
            # Leave the item pending so the caller can retry on the next run.
            logging.getLogger(__name__).warning("Manual sheet snapshot unavailable for %s", vid, exc_info=True)
            continue
        synced = False
        manual_reason = ""
        if targets_before:
            expected_targets = {}
            writes = []
            for target in targets_before:
                expected_source = target["source"] if target["source"].strip() else "own"
                expected_targets[target["row"]] = (expected_source, model)
                writes.append({"range": f"{quoted}!{col_letter(model_col)}{target['row']}", "values": [[model]]})
                if not target["source"].strip():
                    writes.append({"range": f"{quoted}!{col_letter(source_col)}{target['row']}", "values": [["own"]]})
            sent = False
            try:
                request = service.spreadsheets().values().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": writes},
                )
                sent = True
                request.execute()
                post = map_rows(read_columns((url_col, source_col, model_col)))

                # Compare every row and exact URL text; a newly inserted or moved
                # row invalidates the snapshot even if the target now looks right.
                sequence_unchanged = row_sequence(snapshot) == row_sequence(post)
                snapshot_by_id, post_by_id = by_video_id(snapshot), by_video_id(post)
                changed_others = []
                repairs = []
                if sequence_unchanged:
                    post_by_row = {row["row"]: row for row in post}
                    for original in snapshot:
                        if original["video_id"] == vid:
                            continue
                        current = post_by_row.get(original["row"])
                        if current is None or (current["source"], current["download"]) != (original["source"], original["download"]):
                            changed_others.append(original["video_id"] or f"row {original['row']}")
                            repairs.extend(updates_for(original))
                else:
                    for other_vid, originals in snapshot_by_id.items():
                        if other_vid == vid:
                            continue
                        currents = post_by_id.get(other_vid, [])
                        if len(originals) == len(currents) == 1:
                            original, current = originals[0], currents[0]
                            if (current["source"], current["download"]) != (original["source"], original["download"]):
                                changed_others.append(other_vid)
                                repairs.extend(updates_for(original, current["row"]))
                    for other_vid in post_by_id:
                        if other_vid != vid and other_vid not in snapshot_by_id:
                            changed_others.append(other_vid)
                if repairs:
                    service.spreadsheets().values().batchUpdate(
                        spreadsheetId=spreadsheet_id,
                        body={"valueInputOption": "RAW", "data": repairs},
                    ).execute()
                    post = map_rows(read_columns((url_col, source_col, model_col)))
                    sequence_unchanged = row_sequence(snapshot) == row_sequence(post)
                target_afters = [row for row in post if row["video_id"] == vid]
                targets_match = (len(target_afters) == len(expected_targets) and
                                 all(expected_targets.get(row["row"]) == (row["source"], row["download"])
                                     for row in target_afters))
                if sequence_unchanged:
                    post_by_row = {row["row"]: row for row in post}
                    others_unchanged = all(
                        original["video_id"] == vid or
                        (original["row"] in post_by_row and
                         (post_by_row[original["row"]]["source"], post_by_row[original["row"]]["download"]) ==
                         (original["source"], original["download"]))
                        for original in snapshot)
                else:
                    others_unchanged = False
                if changed_others:
                    manual_reason = "other sheet rows changed during write; repair attempted: " + ", ".join(changed_others)
                if not sequence_unchanged:
                    manual_reason = (manual_reason + "; " if manual_reason else "") + "sheet row/video_id sequence or URL text changed during write"
                if (targets_match
                        and not changed_others and others_unchanged and sequence_unchanged):
                    synced = True
                elif not manual_reason:
                    manual_reason = "sheet write readback did not match target or snapshot"
            except Exception as exc:
                if sent:
                    manual_reason = f"uncertain sheet write/readback outcome: {type(exc).__name__}: {exc}"
                else:
                    logging.getLogger(__name__).warning("Manual sheet write not sent for %s", vid, exc_info=True)
                    continue
        else:
            manual_reason = "target video missing from sheet snapshot"
        if not synced:
            ledger.loc[ledger.video_id.astype(str) == vid, "needs_manual_check"] = "true"
            ledger.loc[ledger.video_id.astype(str) == vid, "reason"] = manual_reason
            logging.getLogger(__name__).error("Manual sheet write requires owner check for %s: %s", vid, manual_reason)
        ledger.loc[ledger.video_id.astype(str) == vid, "sheet_synced"] = "true" if synced else "false"
    return ledger


def verified_file_readback(path: str, expected_content: str) -> bool:
    """Confirm the destination bytes represent the content just published."""
    try:
        return Path(path).read_text(encoding="utf-8-sig") == expected_content
    except (OSError, UnicodeError, TypeError):
        return False


def reconcile_staging_flush(ledger, results, expected_contents=None):
    """Reconcile manual pending-drive rows with verified files published by flush_staging."""
    pending = ledger[(ledger.status == "retryable_failed") &
                     ledger.reason.astype(str).str.startswith("pending_drive:")]
    for _, item in pending.iterrows():
        staged_name = Path(str(item.md_path or "")).name
        if not staged_name:
            continue
        for result in results or []:
            if not getattr(result, "drive_ok", False) or Path(str(getattr(result, "drive_path", ""))).name != staged_name:
                continue
            dest = str(result.drive_path)
            try:
                content = Path(dest).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                continue
            # Flush results do not carry a video ID. The exact retained note must
            # identify the ledger video before it can complete that manual row.
            expected = (expected_contents or {}).get(staged_name)
            staged = Path(str(item.md_path or ""))
            expected_hash = str(item.get("md_sha256", "") or "")
            actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if (expected is None or content != expected or not staged.is_file() or
                    not expected_hash or actual_hash != expected_hash or str(item.video_id) not in content):
                continue
            staged.unlink(missing_ok=True)
            ledger = upsert_ledger(ledger, video_id=str(item.video_id), status="success",
                                   reason="", md_path=dest, drive_verified="verified")
            break
    return ledger


def retry_pending_drive(ledger, *, base_path: str, work_path: str | None, md_root: str,
                        publisher=None):
    """Retry retained manual Markdown via Drive publish without running transcription/LLM."""
    from scripts.drive_yt_summary.publish import publish_final_md, staging_root
    publish = publisher or publish_final_md
    staging_base = Path(staging_root(work_path, base_path))
    quarantined = ledger[ledger.status == "pending_quarantine"]
    for _, item in quarantined.iterrows():
        staged = Path(str(item.md_path or ""))
        try:
            staged.relative_to(staging_base)
            in_staging = True
        except ValueError:
            in_staging = False
        if not in_staging or not staged.is_file():
            continue
        try:
            quarantine = Path(work_path or base_path) / "quarantine" / "manual_staging"
            quarantine.mkdir(parents=True, exist_ok=True)
            destination = quarantine / f"{item.video_id}--{staged.name}"
            suffix = 1
            while destination.exists():
                destination = quarantine / f"{item.video_id}--{suffix}--{staged.name}"
                suffix += 1
            staged.replace(destination)
        except OSError:
            logging.getLogger(__name__).exception("Could not quarantine invalid manual staging for %s", item.video_id)
            continue
        mask = ledger.video_id.astype(str) == str(item.video_id)
        ledger.loc[mask, "status"] = "retryable_failed"
        ledger.loc[mask, "reason"] = "staged Markdown missing or hash mismatch; reprocessing required"
        ledger.loc[mask, ["md_path", "md_sha256"]] = ""
        ledger.loc[mask, "drive_verified"] = "false"
    pending = ledger[(ledger.status == "retryable_failed") &
                     ledger.reason.astype(str).str.startswith("pending_drive:")]
    for _, item in pending.iterrows():
        staged = Path(str(item.md_path or ""))
        expected_hash = str(item.get("md_sha256", "") or "")
        try:
            staged.relative_to(staging_base)
            in_staging = True
        except ValueError:
            in_staging = False
        try:
            content = staged.read_text(encoding="utf-8-sig") if in_staging and staged.is_file() else ""
            observed_hash = hashlib.sha256(content.encode("utf-8")).hexdigest() if in_staging and staged.is_file() else ""
        except (OSError, UnicodeError):
            content, observed_hash = "", ""
        if not in_staging or not staged.is_file() or not expected_hash or observed_hash != expected_hash:
            if in_staging and staged.is_file():
                try:
                    quarantine = Path(work_path or base_path) / "quarantine" / "manual_staging"
                    quarantine.mkdir(parents=True, exist_ok=True)
                    destination = quarantine / f"{item.video_id}--{staged.name}"
                    suffix = 1
                    while destination.exists():
                        destination = quarantine / f"{item.video_id}--{suffix}--{staged.name}"
                        suffix += 1
                    staged.replace(destination)
                except OSError:
                    logging.getLogger(__name__).exception("Could not quarantine invalid manual staging for %s", item.video_id)
                    mask = ledger.video_id.astype(str) == str(item.video_id)
                    ledger.loc[mask, "status"] = "pending_quarantine"
                    ledger.loc[mask, "reason"] = "quarantine failed; staging remains blocked"
                    # Preserve the original path and expected hash so the next run
                    # can retry quarantine and publication guards can identify it.
                    continue
            mask = ledger.video_id.astype(str) == str(item.video_id)
            ledger.loc[mask, "reason"] = "staged Markdown missing or hash mismatch; reprocessing required"
            ledger.loc[mask, ["md_path", "md_sha256"]] = ""
            ledger.loc[mask, "drive_verified"] = "false"
            if _attempt_count(item.get("regen_attempts")) >= MAX_REGEN_ATTEMPTS:
                ledger.loc[mask, "reason"] = "regen cap reached"
            continue
        try:
            try:
                rel = staged.relative_to(staging_base).as_posix()
            except ValueError:
                rel = staged.name
            result = publish(rel_path=rel, content=content, staging_path=str(staged),
                             md_root=md_root, mirror=False, base_path=base_path,
                             work_path=work_path, video_id=str(item.video_id), retain_staging=True)
            if result.drive_ok and verified_file_readback(result.drive_path, content):
                ledger = upsert_ledger(ledger, video_id=str(item.video_id), status="success",
                                       reason="", md_path=result.drive_path, drive_verified="verified")
                staged.unlink(missing_ok=True)
            elif result.drive_action == "unchanged":
                result = publish(rel_path=rel, content=content, staging_path=str(staged),
                                 md_root=md_root, mirror=False, base_path=base_path,
                                 work_path=work_path, video_id=str(item.video_id),
                                 retain_staging=True, force_rewrite=True)
                if result.drive_ok and verified_file_readback(result.drive_path, content):
                    ledger = upsert_ledger(ledger, video_id=str(item.video_id), status="success",
                                           reason="", md_path=result.drive_path, drive_verified="verified")
                    staged.unlink(missing_ok=True)
        except Exception:
            # Retain the pending ledger/staging artifact for the next run.
            continue
    return ledger
