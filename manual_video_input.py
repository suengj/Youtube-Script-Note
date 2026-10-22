"""Normalization and local status ledger for owner supplied YouTube URLs."""
from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LEDGER_COLUMNS = ["video_id", "canonical_url", "source_urls", "first_seen_at", "status", "reason", "attempts", "last_attempt_at", "done_at", "md_path", "md_sha256", "drive_verified", "model", "sheet_synced", "needs_manual_check"]
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
                  sheet_synced: str = "", md_sha256: str = "", attempted: bool = False):
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
                         channel_auto_sub_only=None, video_channel_ids=None):
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
        duration_iso = (duration_map or {}).get(vid, "")
        if shorts_threshold_seconds > 0 and duration_iso:
            from channel_crawl import _parse_duration_to_seconds
            seconds = _parse_duration_to_seconds(str(duration_iso))
            if seconds is not None and seconds <= shorts_threshold_seconds:
                ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                       source_url=source_url, status="skipped_policy",
                                       reason=f"Shorts duration policy: {seconds}s <= {shorts_threshold_seconds}s")
                continue
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
            # Preserve manual ownership so channel processing can also finalize the
            # manual ledger. The caller merges this ID with its channel URL and only
            # processes it once; do not overwrite an in-flight Drive retry reason.
            if prior_status == "retryable_failed" and str(prior.iloc[-1].get("reason") or "").startswith("pending_drive:"):
                continue
            if vid not in seen:
                seen.add(vid)
                queued.append((vid, item["canonical_url"], source_url))
                ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                       source_url=source_url, status="processing",
                                       reason="manual ownership retained; selected channel candidate will process once")
            continue
        if output_status in {"passed_shorts", "live_scheduled", "video_unavailable", "skipped_auto_subs_only", "oversized_file"}:
            ledger = upsert_ledger(ledger, video_id=vid, canonical_url=item["canonical_url"],
                                   source_url=source_url, status="unsupported",
                                   reason=f"prior terminal pipeline status: {output_status}")
            continue
        attempts = _attempt_count(prior.iloc[-1].get("attempts")) if len(prior) else 0
        pending_drive = (prior_status == "retryable_failed" and
                         str(prior.iloc[-1].get("reason") or "").startswith("pending_drive:"))
        if pending_drive:
            # retry_pending_drive either publishes the exact staged artifact or
            # clears this marker when it is missing/stale, allowing regeneration.
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
    wanted = ledger[(ledger.status == "success") &
                    (ledger.drive_verified.astype(str) == "verified") &
                    (ledger.sheet_synced.astype(str).str.lower() != "true")]
    def read_columns(columns):
        ranges = [f"{_quoted_tab(tab)}!{col_letter(col)}2:{col_letter(col)}" for col in columns]
        response = service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id, ranges=ranges).execute()
        return [entry.get("values", []) for entry in response.get("valueRanges", [])]

    for _, item in wanted.iterrows():
        vid = str(item.video_id)
        model = str(item.get("model", "") or "").strip()
        if not model:
            continue
        headers = validate_sheet(service, spreadsheet_id, tab)
        columns = {name: headers.index(name) + 1 for name in SHEET_HEADERS}
        url_col, source_col, model_col = (columns[k] for k in SHEET_HEADERS)
        synced = False
        for attempt in range(2):
            snapshot_columns = read_columns((url_col, source_col, model_col))
            urls, sources, downloads = snapshot_columns
            snapshot = {}
            for offset, url_cell in enumerate(urls):
                url = str((url_cell or [""])[0] or "")
                normalized = normalize_youtube_url(url)
                if normalized["kind"] != "video":
                    continue
                row_no = offset + 2
                source = str((sources[offset] or [""])[0] or "") if offset < len(sources) else ""
                download = str((downloads[offset] or [""])[0] or "") if offset < len(downloads) else ""
                snapshot[normalized["video_id"]] = (row_no, source, download)
            target_before = snapshot.get(vid)
            if not target_before:
                break
            target_row, old_source, old_download = target_before
            expected_source = old_source if old_source.strip() else "own"
            quoted = _quoted_tab(tab)
            writes = [{"range": f"{quoted}!{col_letter(model_col)}{target_row}", "values": [[model]]}]
            if not old_source.strip():
                writes.append({"range": f"{quoted}!{col_letter(source_col)}{target_row}", "values": [["own"]]})
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": writes},
            ).execute()

            post_columns = read_columns((url_col, source_col, model_col))
            post_urls, post_sources, post_downloads = post_columns
            post = {}
            for offset, url_cell in enumerate(post_urls):
                url = str((url_cell or [""])[0] or "")
                normalized = normalize_youtube_url(url)
                if normalized["kind"] != "video":
                    continue
                row_no = offset + 2
                source = str((post_sources[offset] or [""])[0] or "") if offset < len(post_sources) else ""
                download = str((post_downloads[offset] or [""])[0] or "") if offset < len(post_downloads) else ""
                post[normalized["video_id"]] = (row_no, source, download)

            # A stale row write may have changed a different video's cells. Restore
            # by current identity and the one pre-write whole-column snapshot.
            repairs = []
            for other_vid, (current_row, current_source, current_download) in post.items():
                if other_vid == vid or other_vid not in snapshot:
                    continue
                _, original_source, original_download = snapshot[other_vid]
                if (current_source, current_download) != (original_source, original_download):
                    repairs.extend([
                        {"range": f"{quoted}!{col_letter(source_col)}{current_row}", "values": [[original_source]]},
                        {"range": f"{quoted}!{col_letter(model_col)}{current_row}", "values": [[original_download]]},
                    ])
            if repairs:
                service.spreadsheets().values().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": repairs},
                ).execute()

            target_after = post.get(vid)
            others_unchanged = all(
                other_vid == vid or (other_vid in post and post[other_vid][1:] == original[1:])
                for other_vid, original in snapshot.items()
            )
            if target_after and target_after[1:] == (expected_source, model) and not repairs and others_unchanged:
                synced = True
                break
        if not synced:
            ledger.loc[ledger.video_id.astype(str) == vid, "needs_manual_check"] = "true"
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
            mask = ledger.video_id.astype(str) == str(item.video_id)
            ledger.loc[mask, "reason"] = "staged Markdown missing or hash mismatch; reprocessing required"
            ledger.loc[mask, ["md_path", "md_sha256"]] = ""
            ledger.loc[mask, "drive_verified"] = "false"
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
