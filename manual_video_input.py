"""Normalization and local status ledger for owner supplied YouTube URLs."""
from __future__ import annotations

import csv
import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LEDGER_COLUMNS = ["video_id", "canonical_url", "source_urls", "first_seen_at", "status", "reason", "attempts", "last_attempt_at", "done_at", "md_path", "drive_verified", "model", "sheet_synced"]
LEDGER_STATUSES = {"new", "processing", "success", "retryable_failed", "unsupported", "skipped_policy"}
SHEET_ID = "1iRtWdbbJGM8EY1K0jDXkt36IImQM-BmhDoufsFOjRKI"
SHEET_TAB = "YT Link"
SHEET_HEADERS = ("URL", "Source", "Download 여부")
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


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


def read_ledger(path: str | Path):
    import pandas as pd
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    try:
        df = pd.read_csv(p, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(p, encoding="cp949")
    for col in LEDGER_COLUMNS:
        if col not in df.columns:
            df[col] = ""
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
                  sheet_synced: str = "", attempted: bool = False):
    if status not in LEDGER_STATUSES:
        raise ValueError(f"invalid manual ledger status: {status}")
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
                           "md_path": md_path, "drive_verified": drive_verified, "model": model,
                           "sheet_synced": sheet_synced or "false"}
    return df


def prepare_manual_queue(manual_rows, channel_urls, output_df, ledger, max_retries: int = 3):
    """Return unique canonical manual items and the ledger with pre-run decisions recorded."""
    channel_ids = set()
    for url in channel_urls:
        item = normalize_youtube_url(url)
        if item["kind"] == "video":
            channel_ids.add(item["video_id"])
    output_statuses = {}
    if output_df is not None and "v_id" in output_df.columns and "status" in output_df.columns:
        for _, row in output_df.iterrows():
            vid = str(row.get("v_id", "")).strip()
            if vid:
                output_statuses.setdefault(vid, []).append(str(row.get("status", "")).strip())
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
        prior = ledger[ledger.video_id.astype(str) == vid]
        prior_status = str(prior.iloc[-1].get("status") or "") if len(prior) else ""
        if prior_status == "unsupported":
            ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                   source_url=source_url, status="unsupported",
                                   reason=str(prior.iloc[-1].get("reason") or "previously classified unsupported"))
            continue
        output_status = output_statuses.get(vid, [""])[-1]
        if prior_status == "success":
            ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                   source_url=source_url, status="success",
                                   reason="already verified by manual ledger")
            continue
        if "success" in output_statuses.get(vid, []) and prior_status != "retryable_failed":
            ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                   source_url=source_url, status="skipped_policy",
                                   reason="already has an output success without manual Drive verification evidence")
            continue
        if vid in channel_ids:
            ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                   source_url=source_url, status="skipped_policy",
                                   reason="video already selected as a channel candidate; channel metadata takes precedence")
            continue
        if output_status in {"passed_shorts", "live_scheduled", "video_unavailable", "skipped_auto_subs_only", "oversized_file"}:
            ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                   source_url=source_url, status="unsupported",
                                   reason=f"prior terminal pipeline status: {output_status}")
            continue
        attempts = _attempt_count(prior.iloc[-1].get("attempts")) if len(prior) else 0
        if (prior_status == "retryable_failed" and
                str(prior.iloc[-1].get("reason") or "").startswith("pending_drive:")):
            # Preserve the generated staging artifact as the sole retry source.
            continue
        if prior_status == "retryable_failed" and attempts >= max_retries:
            ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                   source_url=source_url, status="retryable_failed",
                                   reason=f"retry limit reached ({max_retries})")
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
    def col_letter(n):
        result = ""
        while n:
            n, rem = divmod(n - 1, 26)
            result = chr(65 + rem) + result
        return result
    headers, values = read_sheet_rows(service, spreadsheet_id, tab)
    columns = {name: headers.index(name) + 1 for name in SHEET_HEADERS}
    url_col, source_col, model_col = (columns[k] for k in SHEET_HEADERS)
    wanted = ledger[(ledger.status == "success") &
                    (ledger.drive_verified.astype(str) == "verified") &
                    (ledger.sheet_synced.astype(str).str.lower() != "true")]
    batch = []
    pending = {}
    for _, item in wanted.iterrows():
        vid = str(item.video_id)
        model = str(item.get("model", "") or "").strip()
        if not model:
            continue
        matches = []
        for row_no, row in enumerate(values[1:], start=2):
            url = row[url_col - 1] if len(row) >= url_col else ""
            normalized = normalize_youtube_url(url)
            if normalized["kind"] == "video" and normalized["video_id"] == vid:
                matches.append((row_no, row))
        if not matches:
            continue
        # All duplicate URL rows for the same normalized video are updated safely.
        pending[vid] = []
        for row_no, row in matches:
            model_range = f"{_quoted_tab(tab)}!{col_letter(model_col)}{row_no}"
            batch.append({"range": model_range, "values": [[model]]})
            pending[vid].append((model_range, model))
            source = row[source_col - 1] if len(row) >= source_col else ""
            if not str(source).strip():
                source_range = f"{_quoted_tab(tab)}!{col_letter(source_col)}{row_no}"
                batch.append({"range": source_range, "values": [["own"]]})
                pending[vid].append((source_range, "own"))
    if not batch:
        return ledger
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": batch},
    ).execute()
    for vid, cells in pending.items():
        matches = True
        for cell_range, expected in cells:
            actual = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=cell_range).execute().get("values", [])
            observed = actual[0][0] if actual and actual[0] else ""
            if observed != expected:
                matches = False
        ledger.loc[ledger.video_id.astype(str) == vid, "sheet_synced"] = "true" if matches else "false"
    return ledger


def retry_pending_drive(ledger, *, base_path: str, work_path: str | None, md_root: str,
                        publisher=None):
    """Retry retained manual Markdown via Drive publish without running transcription/LLM."""
    from scripts.drive_yt_summary.publish import publish_final_md, staging_root
    publish = publisher or publish_final_md
    root = staging_root(work_path, base_path)
    pending = ledger[(ledger.status == "retryable_failed") &
                     ledger.reason.astype(str).str.startswith("pending_drive:")]
    for _, item in pending.iterrows():
        staged = Path(str(item.md_path or ""))
        if not staged.is_file():
            continue
        try:
            content = staged.read_text(encoding="utf-8-sig")
            rel = staged.relative_to(root).as_posix()
            result = publish(rel_path=rel, content=content, staging_path=str(staged),
                             md_root=md_root, mirror=False, base_path=base_path,
                             work_path=work_path, video_id=str(item.video_id))
            if result.drive_ok:
                ledger = upsert_ledger(ledger, video_id=str(item.video_id), status="success",
                                       reason="", md_path=result.drive_path, drive_verified="verified")
        except Exception:
            # Retain the pending ledger/staging artifact for the next run.
            continue
    return ledger
