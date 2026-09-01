"""
채널 기반 증분 크롤 (channel_crawl)
config CHANNEL_CRAWL=true 시 channel_df.csv 채널별 last_processed 이후
신규 영상만 수집하고, output_df_new에 이미 있는 v_id는 제외한 URL 리스트를 반환합니다.
YouTube Data API v3 사용 (YOUTUBE_API_KEY 필요). backfill/구간 지원.

경로: 함수의 첫 인자(코드상 이름 ``base_path``)는 **프로젝트 루트가 아니라 DATA_ROOT**
(``input_df.csv``, ``output_df_new.csv``, ``channel_df.csv``, ``crawl_yt_list.csv`` 가 있는 디렉터리)를 넘깁니다.
main.py는 ``config['DATA_ROOT']``를 전달합니다.
"""
import os
import sys
import re
import csv
import logging
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

# launchd 환경에서 한글 출력을 위한 인코딩 설정
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    import io
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

try:
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError
    from urllib.parse import urlencode, unquote, quote
except ImportError:
    Request = urlopen = urlencode = unquote = quote = None  # type: ignore

logger = logging.getLogger(__name__)

# YouTube Data API v3
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
CHANNEL_DF_FILENAME = "channel_df.csv"
CRAWL_QUEUE_FILENAME = "crawl_yt_list.csv"
CHANNEL_DF_COLUMNS = [
    "channel_url",
    "channel_name",
    "usage_channel",
    "channel_id",           # UCxxx (캐시, extract/API 생략용)
    "uploads_playlist_id",  # UUxxx (캐시, channels.list 생략용)
    "last_processed_published_at",
    "last_discovered_published_at",
    "auto_sub_only",
]
CRAWL_QUEUE_COLUMNS = [
    "video_id",
    "url",
    "channel_id",
    "channel_url",
    "channel_name",
    "usage_channel",
    "published_at",
    "status",
    "retry_count",
    "last_error",
    "discovered_at",
    "last_attempted_at",
    "done_at",
    "is_shorts",
    "duration_iso",
    "default_audio_lang",
    "auto_sub_only",
]
DONE_STATUSES = {"success", "already_existed", "oversized_file"}
SKIPPED_AUTO_SUBS_ONLY_STATUS = "skipped_auto_subs_only"
FAILED_STATUSES = {"error", "download_failed", "mlx_error", "api_error", "file_error"}
SHORTS_STATUS = "passed_shorts"
LIVE_SCHEDULED_STATUS = "live_scheduled"
VIDEO_UNAVAILABLE_STATUS = "video_unavailable"
# /channel/UCxxx (24 chars: UC + 22 base64url)
CHANNEL_URL_ID_PATTERN = re.compile(r"youtube\.com/channel/(UC[\w-]{22})", re.IGNORECASE)
# @handle: youtube.com/@Handle or youtube.com/@Handle/videos (한글 등 모든 문자 포함, /?&# 전까지)
HANDLE_URL_PATTERN = re.compile(r"youtube\.com/@([^/?#\s]+)", re.IGNORECASE)
# Canonical link: points to THIS page's channel (most reliable; avoids picking a related/recommended channel ID)
CHANNEL_ID_CANONICAL_LINK_PATTERN = re.compile(
    r'<link\s+rel="canonical"\s+href="https?://(?:www\.)?youtube\.com/channel/(UC[\w-]{22})"',
    re.IGNORECASE,
)
# JSON canonicalBaseUrl: same idea (page's own channel)
CHANNEL_ID_CANONICAL_BASEURL_PATTERN = re.compile(
    r'"canonicalBaseUrl"\s*:\s*"https://www\.youtube\.com/channel/(UC[\w-]{22})"',
)
# Fallback: first channelId/externalId in page (can be wrong if another channel appears earlier in HTML)
CHANNEL_ID_IN_PAGE_PATTERN = re.compile(r'"(?:channelId|externalId)"\s*:\s*"(UC[\w-]{22})"')


def extract_channel_id_from_url(channel_url: str) -> Optional[str]:
    """
    Extract channel_id from YouTube channel URL.
    - https://www.youtube.com/channel/UCxxx → returns UCxxx
    - https://www.youtube.com/@Handle or /@Handle/videos → resolves via channel page, returns UCxxx or None
    URL 인코딩된 handle(%EA%B9%80... 등)도 처리함.
    """
    if not channel_url or not channel_url.strip():
        return None
    url = channel_url.strip()
    # URL 디코딩 (인코딩된 @handle 처리)
    try:
        url_decoded = unquote(url) if unquote else url
    except Exception:
        url_decoded = url
    m = CHANNEL_URL_ID_PATTERN.search(url_decoded)
    if m:
        return m.group(1)
    handle_m = HANDLE_URL_PATTERN.search(url_decoded)
    if handle_m:
        handle = handle_m.group(1)
        # handle도 디코딩 (한글 등이 %XX로 인코딩된 경우)
        try:
            handle_decoded = unquote(handle) if unquote else handle
        except Exception:
            handle_decoded = handle
        return _resolve_handle_to_channel_id(handle_decoded)
    return None


def _resolve_handle_to_channel_id(handle: str) -> Optional[str]:
    """Resolve @handle to channel_id by fetching the channel page and parsing channelId from HTML."""
    handle = (handle or "").strip().lstrip("@")
    if not handle:
        return None
    # urllib Request expects ASCII-safe URL; non-ASCII handles must be percent-encoded.
    encoded_handle = quote(handle, safe="") if quote else handle
    url = f"https://www.youtube.com/@{encoded_handle}"
    logger.info("channel_crawl: resolving handle @%s", handle)
    try:
        req = Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:91.0) Gecko/20100101 Firefox/91.0"},
        )
        with urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # Prefer canonical link: it points to THIS page's channel (avoids wrong ID from related/recommended channels)
        found = CHANNEL_ID_CANONICAL_LINK_PATTERN.search(html)
        if found:
            return found.group(1)
        # Then canonicalBaseUrl in JSON (same meaning)
        found = CHANNEL_ID_CANONICAL_BASEURL_PATTERN.search(html)
        if found:
            return found.group(1)
        # Last resort: first channelId/externalId in page (may be wrong if another channel appears earlier)
        found = CHANNEL_ID_IN_PAGE_PATTERN.search(html)
        if found:
            return found.group(1)
    except (URLError, HTTPError, OSError, UnicodeError, ValueError) as e:
        # handle에 한글이 포함될 수 있으므로 안전하게 처리
        try:
            safe_handle = str(handle).encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            safe_error = str(e).encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            logger.warning("Failed to resolve @%s to channel_id: %s", safe_handle, safe_error)
        except Exception:
            logger.warning("Failed to resolve handle to channel_id: [encoding error]")
    return None


def _parse_iso_date(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO 8601 date string to datetime. Returns None on failure."""
    if not s or not s.strip():
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
        try:
            if fmt.endswith("%z"):
                return datetime.strptime(s.replace("Z", "+00:00").replace("+00:00", ""), "%Y-%m-%dT%H:%M:%S")
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _is_valid_channel_id(s: str) -> bool:
    """UC + 22 chars = 24 total."""
    return bool(s and len(s) == 24 and s.startswith("UC") and s[2:].replace("-", "").replace("_", "").isalnum())


def _is_valid_uploads_playlist_id(s: str) -> bool:
    """UU + 22 chars = 24 total."""
    return bool(s and len(s) == 24 and s.startswith("UU") and s[2:].replace("-", "").replace("_", "").isalnum())


def load_channel_df(base_path: str) -> List[Dict[str, Any]]:
    """
    Load channel_df.csv. Returns list of dicts with channel_url, channel_name, channel_id, uploads_playlist_id, etc.
    Uses cached channel_id/uploads_playlist_id from CSV when valid; otherwise resolves from URL/API.
    Rows whose channel_url does not yield a channel_id are skipped.
    """
    path = os.path.join(base_path, CHANNEL_DF_FILENAME)
    if not os.path.exists(path):
        logger.warning(f"channel_df.csv not found: {path}")
        return []
    rows = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)
        total_rows = len(all_rows)
        logger.info("channel_crawl: loading %d rows from channel_df.csv", total_rows)
        for idx, row in enumerate(all_rows, start=1):
            url_s = (row.get("channel_url") or "").strip()
            if not url_s:
                continue
            logger.info("channel_crawl: parsing channel row [%d/%d] url=%s", idx, total_rows, url_s[:80])
            cid_cached = (row.get("channel_id") or "").strip()
            if _is_valid_channel_id(cid_cached):
                cid = cid_cached
            else:
                cid = extract_channel_id_from_url(url_s)
            if not cid:
                try:
                    safe_url = url_s[:60].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                    logger.warning("channel_df: skipping row (could not extract channel_id from URL): %s...", safe_url)
                except Exception:
                    logger.warning("channel_df: skipping row (could not extract channel_id from URL): [URL encoding error]")
                continue
            uploads_cached = (row.get("uploads_playlist_id") or "").strip()
            uploads_id = uploads_cached if _is_valid_uploads_playlist_id(uploads_cached) else ""
            rows.append({
                "channel_url": url_s,
                "channel_name": (row.get("channel_name") or "").strip(),
                "usage_channel": (row.get("usage_channel") or "").strip(),
                "channel_id": cid,
                "uploads_playlist_id": uploads_id,
                "last_processed_published_at": (row.get("last_processed_published_at") or "").strip(),
                "last_discovered_published_at": (row.get("last_discovered_published_at") or "").strip(),
                "auto_sub_only": (row.get("auto_sub_only") or "").strip(),
            })
    return rows


def save_channel_df(base_path: str, rows: List[Dict[str, Any]]) -> None:
    """Save channel_df.csv with dual cursors (processed/discovered) and cached channel_id/uploads_playlist_id."""
    path = os.path.join(base_path, CHANNEL_DF_FILENAME)
    out_rows = [
        {
            "channel_url": r.get("channel_url", ""),
            "channel_name": r.get("channel_name", ""),
            "usage_channel": r.get("usage_channel", ""),
            "channel_id": r.get("channel_id", ""),
            "uploads_playlist_id": r.get("uploads_playlist_id", ""),
            "last_processed_published_at": r.get("last_processed_published_at", ""),
            "last_discovered_published_at": r.get("last_discovered_published_at", ""),
            "auto_sub_only": r.get("auto_sub_only", ""),
        }
        for r in rows
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CHANNEL_DF_COLUMNS)
        w.writeheader()
        w.writerows(out_rows)


def _api_request(api_key: str, path: str, params: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """GET YouTube Data API v3; returns JSON as dict or None on failure."""
    url = f"{YOUTUBE_API_BASE}/{path}?{urlencode({**params, 'key': api_key})}"
    try:
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, OSError, json.JSONDecodeError) as e:
        logger.warning(f"YouTube API request failed: {e}")
        return None


def _chunked(items: List[str], n: int) -> List[List[str]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_duration_to_seconds(duration_iso: str) -> Optional[int]:
    """
    Parse ISO 8601 duration (PT#H#M#S) to seconds.
    """
    if not duration_iso or not duration_iso.startswith("PT"):
        return None
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_iso)
    if not m:
        return None
    h = int(m.group(1) or 0)
    mm = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    return h * 3600 + mm * 60 + s


def _fetch_video_durations(api_key: str, video_ids: List[str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Call videos.list(part=contentDetails,snippet) in batches(<=50).
    Returns (duration_map, default_audio_lang_map).
    duration_map: {video_id: duration_iso}
    default_audio_lang_map: {video_id: defaultAudioLanguage or ""}
    Quota note: videos.list is quota-based, not IP-block based.
    """
    duration_map: Dict[str, str] = {}
    default_audio_lang_map: Dict[str, str] = {}
    for chunk in _chunked(video_ids, 50):
        data = _api_request(
            api_key,
            "videos",
            {"part": "contentDetails,snippet", "id": ",".join(chunk), "maxResults": "50"},
        )
        if not data:
            continue
        for it in data.get("items", []):
            vid = it.get("id")
            if not vid:
                continue
            vid = str(vid)
            dur = (it.get("contentDetails") or {}).get("duration") or ""
            if dur:
                duration_map[vid] = str(dur)
            default_lang = (it.get("snippet") or {}).get("defaultAudioLanguage") or ""
            default_audio_lang_map[vid] = str(default_lang)
    return duration_map, default_audio_lang_map


def _queue_path(base_path: str) -> str:
    return os.path.join(base_path, CRAWL_QUEUE_FILENAME)


def load_crawl_queue_df(base_path: str):
    import pandas as pd
    path = _queue_path(base_path)
    if not os.path.exists(path):
        return pd.DataFrame(columns=CRAWL_QUEUE_COLUMNS)
    try:
        for attempt in range(3):
            try:
                df = pd.read_csv(path, encoding="utf-8-sig")
                break
            except OSError as e:
                if getattr(e, "errno", None) == 11 and attempt < 2:
                    import time
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
        for col in CRAWL_QUEUE_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[CRAWL_QUEUE_COLUMNS].copy()
    except Exception as e:
        logger.warning("crawl_queue: failed to read queue csv (%s). starting empty queue.", e)
        return pd.DataFrame(columns=CRAWL_QUEUE_COLUMNS)


def save_crawl_queue_df(base_path: str, queue_df) -> None:
    """Save queue CSV via temp file + atomic replace to avoid iCloud lock (Errno 11)."""
    path = _queue_path(base_path)
    tmp_path = path + ".tmp"
    q = queue_df.copy()
    for col in CRAWL_QUEUE_COLUMNS:
        if col not in q.columns:
            q[col] = ""
    q = q[CRAWL_QUEUE_COLUMNS]
    max_retries = 3
    for attempt in range(max_retries):
        try:
            q.to_csv(tmp_path, index=False, encoding="utf-8-sig")
            os.replace(tmp_path, path)
            return
        except OSError as e:
            if getattr(e, "errno", None) == 11 and attempt < max_retries - 1:
                import time
                time.sleep(2 * (attempt + 1))
                continue
            raise


def reconcile_queue_with_output_df(queue_df, output_df):
    """
    output_df_new.csv 상태를 기준으로 queue 상태를 보정.
    """
    if queue_df is None or len(queue_df) == 0 or output_df is None or len(output_df) == 0:
        return queue_df
    if "v_id" not in output_df.columns or "status" not in output_df.columns:
        return queue_df

    out = output_df[["v_id", "status"]].copy()
    out["v_id"] = out["v_id"].astype(str).str.strip()
    out["status"] = out["status"].astype(str).str.strip()
    out = out.drop_duplicates(subset=["v_id"], keep="last")
    status_map = dict(zip(out["v_id"], out["status"]))

    now_iso = _now_iso()
    q = queue_df.copy()
    q["video_id"] = q["video_id"].astype(str).str.strip()
    q["status"] = q["status"].astype(str).str.strip()

    for idx, row in q.iterrows():
        vid = row.get("video_id", "")
        if not vid or vid not in status_map:
            continue
        st = status_map[vid]
        if st in DONE_STATUSES or st == SHORTS_STATUS or st == LIVE_SCHEDULED_STATUS or st == VIDEO_UNAVAILABLE_STATUS or st == SKIPPED_AUTO_SUBS_ONLY_STATUS:
            q.at[idx, "status"] = "done"
            if not str(q.at[idx, "done_at"]).strip():
                q.at[idx, "done_at"] = now_iso
            q.at[idx, "last_error"] = ""
        elif st in FAILED_STATUSES and str(q.at[idx, "status"]).strip() != "done":
            q.at[idx, "status"] = "failed"
            if not str(q.at[idx, "last_attempted_at"]).strip():
                q.at[idx, "last_attempted_at"] = now_iso
            if not str(q.at[idx, "last_error"]).strip():
                q.at[idx, "last_error"] = st
    return q


def select_process_candidates(queue_df, max_retries: int):
    if queue_df is None or len(queue_df) == 0:
        return queue_df
    q = queue_df.copy()
    q["status"] = q["status"].astype(str).str.strip()
    q["retry_count"] = q["retry_count"].fillna(0).astype(int)
    cand = q[(q["status"] == "queued") | ((q["status"] == "failed") & (q["retry_count"] < max_retries))].copy()
    if len(cand) == 0:
        return cand
    # deterministic order: oldest published first, then discovery time
    cand["_sort_pub"] = cand["published_at"].astype(str)
    cand["_sort_disc"] = cand["discovered_at"].astype(str)
    cand = cand.sort_values(by=["_sort_pub", "_sort_disc"]).drop(columns=["_sort_pub", "_sort_disc"])
    return cand


def apply_result_to_queue(queue_df, video_id: str, status: str, error_msg: Optional[str]):
    if queue_df is None or len(queue_df) == 0 or not video_id:
        return queue_df
    q = queue_df.copy()
    q["video_id"] = q["video_id"].astype(str).str.strip()
    idx_list = q.index[q["video_id"] == str(video_id).strip()].tolist()
    if not idx_list:
        return q
    idx = idx_list[-1]
    now_iso = _now_iso()
    q.at[idx, "last_attempted_at"] = now_iso

    if status in DONE_STATUSES or status == SHORTS_STATUS or status == LIVE_SCHEDULED_STATUS or status == VIDEO_UNAVAILABLE_STATUS or status == SKIPPED_AUTO_SUBS_ONLY_STATUS:
        q.at[idx, "status"] = "done"
        q.at[idx, "done_at"] = now_iso
        q.at[idx, "last_error"] = ""
    else:
        q.at[idx, "status"] = "failed"
        try:
            rc = int(q.at[idx, "retry_count"] or 0)
        except Exception:
            rc = 0
        q.at[idx, "retry_count"] = rc + 1
        q.at[idx, "last_error"] = (error_msg or status or "failed")
    return q


def update_channel_last_processed_from_queue(base_path: str, queue_df=None) -> None:
    """
    Update channel_df.last_processed_published_at based on queue rows with status=done.
    """
    import pandas as pd
    rows = load_channel_df(base_path)
    if not rows:
        return
    q = queue_df if queue_df is not None else load_crawl_queue_df(base_path)
    if q is None or len(q) == 0:
        return
    q = q.copy()
    q["status"] = q["status"].astype(str).str.strip()
    q["channel_id"] = q["channel_id"].astype(str).str.strip()
    q["published_at"] = q["published_at"].astype(str).str.strip()
    done = q[q["status"] == "done"]
    if len(done) == 0:
        return

    channel_max: Dict[str, datetime] = {}
    for _, r in done.iterrows():
        cid = r.get("channel_id", "")
        pub = r.get("published_at", "")
        if not cid or not pub:
            continue
        dt = _parse_iso_date(pub)
        if not dt:
            continue
        if cid not in channel_max or dt > channel_max[cid]:
            channel_max[cid] = dt

    for ch in rows:
        cid = ch.get("channel_id", "")
        if cid not in channel_max:
            continue
        current = _parse_iso_date((ch.get("last_processed_published_at") or "").strip())
        if not current or channel_max[cid] > current:
            ch["last_processed_published_at"] = channel_max[cid].strftime("%Y-%m-%dT%H:%M:%SZ")
    save_channel_df(base_path, rows)


def fetch_channel_via_api(
    api_key: str,
    channel_id: str,
    uploads_id: Optional[str] = None,
    cursor_dt: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], str, str]:
    """
    Fetch channel uploads via YouTube Data API v3.
    Returns (entries, channel_title, uploads_id). entries: list of {video_id, url, published_at, channel_id}.
    - uploads_id: if provided, skip channels.list (1 API call saved).
    - cursor_dt: when set (non-backfill), stop paginating when all items on a page have published_at <= cursor_dt.
    """
    channel_title = ""
    if not uploads_id or not _is_valid_uploads_playlist_id(uploads_id):
        data = _api_request(api_key, "channels", {"part": "contentDetails,snippet", "id": channel_id})
        if not data or "items" not in data or not data["items"]:
            logger.warning(f"YouTube API: no channel found for {channel_id}")
            return [], "", ""
        item = data["items"][0]
        uploads_id = (item.get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads")
        channel_title = (item.get("snippet") or {}).get("title") or ""
        if not uploads_id:
            logger.warning(f"YouTube API: no uploads playlist for {channel_id}")
            return [], channel_title, ""

    entries = []
    page_token = ""
    while True:
        params = {"part": "snippet", "playlistId": uploads_id, "maxResults": "50"}
        if page_token:
            params["pageToken"] = page_token
        data = _api_request(api_key, "playlistItems", params)
        if not data or "items" not in data:
            break
        items = data.get("items", [])
        all_past_cursor = True
        for pl_item in items:
            sn = pl_item.get("snippet") or {}
            rid = sn.get("resourceId") or {}
            video_id = rid.get("videoId")
            if not video_id:
                continue
            published = sn.get("publishedAt") or ""
            pub_dt = _parse_iso_date(published) if published else None
            if cursor_dt and pub_dt and pub_dt > cursor_dt:
                all_past_cursor = False
            if not channel_title:
                channel_title = sn.get("channelTitle") or sn.get("videoOwnerChannelTitle") or ""
            entries.append({
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published_at": published,
                "channel_id": channel_id,
            })
        if cursor_dt and all_past_cursor and items:
            break
        page_token = data.get("nextPageToken") or ""
        if not page_token:
            break
    return entries, channel_title, uploads_id or ""


def get_url_list_from_channel_crawl(
    base_path: str,
    config: dict,
    output_df,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """
    Build URL list from channel crawl (RSS + last_processed + optional backfill).
    Excludes video IDs already in output_df.
    Returns (url_list, meta_list) where meta_list[i] = {url, published_at, channel_id} for url_list[i].
    """
    import pandas as pd
    channels = load_channel_df(base_path)
    if not channels:
        logger.info("channel_crawl: no channels loaded from channel_df.csv")
        return [], []
    logger.info("channel_crawl: loaded %d channels from channel_df.csv", len(channels))

    backfill = config.get("CHANNEL_BACKFILL") or False
    start_date_s = (config.get("CHANNEL_START_DATE") or "").strip()
    end_date_s = (config.get("CHANNEL_END_DATE") or "").strip()
    if backfill and not end_date_s:
        raise ValueError("CHANNEL_BACKFILL=true requires CHANNEL_END_DATE to be set.")

    done_v_ids = set()
    if output_df is not None and hasattr(output_df, "columns") and "v_id" in output_df.columns:
        done_v_ids = set(output_df["v_id"].astype(str).str.strip())

    all_meta = []
    for idx, ch in enumerate(channels, start=1):
        cid = ch["channel_id"]
        ch_name = (ch.get("channel_name") or "").strip() or "(no_name)"
        ch_url = (ch.get("channel_url") or "").strip()
        last_s = (ch.get("last_processed_published_at") or "").strip()
        last_dt = _parse_iso_date(last_s) if last_s else None
        logger.info(
            "channel_crawl: [%d/%d] channel=%s cid=%s last_processed=%s",
            idx,
            len(channels),
            ch_name,
            cid,
            last_s or "(empty)",
        )
        if ch_url:
            logger.info("channel_crawl:   source channel_url=%s", ch_url)

        if not backfill and not last_s:
            logger.warning(
                "channel_df: last_processed_published_at is required when CHANNEL_BACKFILL is false; "
                "skipping channel: %s",
                ch.get("channel_url", cid)[:80],
            )
            continue

        api_key = (config.get("YOUTUBE_API_KEY") or "").strip()
        if not api_key:
            raise ValueError("CHANNEL_CRAWL requires YOUTUBE_API_KEY in .env. See docs/YOUTUBE_API_SETUP.md")
        uploads_id = ch.get("uploads_playlist_id") or None
        if uploads_id and not _is_valid_uploads_playlist_id(uploads_id):
            uploads_id = None
        entries, feed_title, uploads_id_returned = fetch_channel_via_api(
            api_key, cid, uploads_id=uploads_id, cursor_dt=last_dt if not backfill else None
        )
        if uploads_id_returned:
            ch["uploads_playlist_id"] = uploads_id_returned
        logger.info("channel_crawl:   fetched %d videos from API for cid=%s", len(entries), cid)
        if not ch.get("channel_name") and feed_title:
            ch["channel_name"] = feed_title
        accepted_before = len(all_meta)
        for e in entries:
            if e["video_id"] in done_v_ids:
                continue
            pub_s = e.get("published_at") or ""
            pub_dt = _parse_iso_date(pub_s) if pub_s else None

            if not backfill:
                if last_dt and pub_dt and pub_dt <= last_dt:
                    continue
            else:
                end_dt = _parse_iso_date(end_date_s)
                if not end_dt:
                    continue
                if pub_dt and pub_dt > end_dt:
                    continue
                if start_date_s:
                    start_dt = _parse_iso_date(start_date_s)
                    if start_dt and pub_dt and pub_dt < start_dt:
                        continue
            all_meta.append(e)
        accepted_now = len(all_meta) - accepted_before
        logger.info("channel_crawl:   accepted %d videos after filters for cid=%s", accepted_now, cid)

    # Persist channel_name filled from RSS (for rows that had empty channel_name)
    save_channel_df(base_path, channels)

    # Dedupe by video_id, keep first (oldest published if sorted)
    seen = set()
    unique = []
    for m in all_meta:
        vid = m["video_id"]
        if vid in seen:
            continue
        seen.add(vid)
        unique.append(m)

    # Sort by published_at ascending (oldest first)
    def _sort_key(m):
        dt = _parse_iso_date(m.get("published_at") or "")
        return (dt or datetime.min).timestamp()

    before_dedupe = len(all_meta)
    unique.sort(key=_sort_key)
    url_list = [m["url"] for m in unique]
    meta_list = [{"url": m["url"], "published_at": m.get("published_at", ""), "channel_id": m["channel_id"]} for m in unique]
    logger.info(
        "channel_crawl: total selected videos=%d (before_dedupe=%d, after_dedupe=%d)",
        len(url_list),
        before_dedupe,
        len(unique),
    )
    if url_list:
        preview_n = min(10, len(url_list))
        logger.info("channel_crawl: URL preview (first %d)", preview_n)
        for i in range(preview_n):
            m = meta_list[i]
            logger.info(
                "channel_crawl:   [%d] %s | published_at=%s | channel_id=%s",
                i + 1,
                m["url"],
                m.get("published_at", ""),
                m.get("channel_id", ""),
            )
    return url_list, meta_list


def build_queue_and_get_candidates(
    base_path: str,
    config: dict,
    output_df,
):
    """
    Channel crawl discovery -> queue persistence -> candidate selection.
    Returns (queue_df, candidates_df, shorts_rows_for_output_df).
    """
    import pandas as pd

    channels = load_channel_df(base_path)
    if not channels:
        logger.info("channel_crawl: no channels loaded from channel_df.csv")
        q = load_crawl_queue_df(base_path)
        q = reconcile_queue_with_output_df(q, output_df)
        save_crawl_queue_df(base_path, q)
        return q, select_process_candidates(q, int(config.get("CRAWL_QUEUE_MAX_RETRIES", 3))), []

    logger.info("channel_crawl: loaded %d channels from channel_df.csv", len(channels))
    queue_df = load_crawl_queue_df(base_path)
    queue_df = reconcile_queue_with_output_df(queue_df, output_df)

    backfill = bool(config.get("CHANNEL_BACKFILL") or False)
    start_date_s = (config.get("CHANNEL_START_DATE") or "").strip()
    end_date_s = (config.get("CHANNEL_END_DATE") or "").strip()
    if backfill and not end_date_s:
        raise ValueError("CHANNEL_BACKFILL=true requires CHANNEL_END_DATE to be set.")

    shorts_minutes = int(config.get("FILTERING_SHORTS_MINUTES", 3) or 0)
    shorts_seconds_threshold = shorts_minutes * 60
    max_retries = int(config.get("CRAWL_QUEUE_MAX_RETRIES", 3) or 3)

    api_key = (config.get("YOUTUBE_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("CHANNEL_CRAWL requires YOUTUBE_API_KEY in .env. See docs/YOUTUBE_API_SETUP.md")

    done_v_ids = set()
    if output_df is not None and hasattr(output_df, "columns") and "v_id" in output_df.columns:
        done_v_ids = set(output_df["v_id"].astype(str).str.strip())

    queue_video_ids = set()
    if queue_df is not None and len(queue_df) > 0 and "video_id" in queue_df.columns:
        queue_video_ids = set(queue_df["video_id"].astype(str).str.strip())

    discovered_rows: List[Dict[str, Any]] = []
    shorts_rows_for_output_df: List[Dict[str, str]] = []
    now_iso = _now_iso()

    for idx, ch in enumerate(channels, start=1):
        cid = ch["channel_id"]
        ch_name = (ch.get("channel_name") or "").strip() or "(no_name)"
        ch_usage = (ch.get("usage_channel") or "").strip()
        ch_url = (ch.get("channel_url") or "").strip()
        last_processed_s = (ch.get("last_processed_published_at") or "").strip()
        last_discovered_s = (ch.get("last_discovered_published_at") or "").strip()

        logger.info(
            "channel_crawl: [%d/%d] channel=%s cid=%s last_processed=%s last_discovered=%s",
            idx,
            len(channels),
            ch_name,
            cid,
            last_processed_s or "(empty)",
            last_discovered_s or "(empty)",
        )
        if ch_url:
            logger.info("channel_crawl:   source channel_url=%s", ch_url)

        if not backfill and not last_discovered_s and not last_processed_s:
            logger.warning(
                "channel_df: last_processed_published_at/last_discovered_published_at is required when CHANNEL_BACKFILL is false; "
                "skipping channel: %s",
                ch.get("channel_url", cid)[:80],
            )
            continue

        cursor_dt = _parse_iso_date(last_discovered_s) if last_discovered_s else None
        if not cursor_dt and last_processed_s:
            cursor_dt = _parse_iso_date(last_processed_s)
        uploads_id = ch.get("uploads_playlist_id") or None
        if uploads_id and not _is_valid_uploads_playlist_id(uploads_id):
            uploads_id = None

        entries, feed_title, uploads_id_returned = fetch_channel_via_api(
            api_key, cid, uploads_id=uploads_id, cursor_dt=cursor_dt if not backfill else None
        )
        if uploads_id_returned:
            ch["uploads_playlist_id"] = uploads_id_returned
        logger.info("channel_crawl:   fetched %d videos from API for cid=%s", len(entries), cid)
        if not ch.get("channel_name") and feed_title:
            ch["channel_name"] = feed_title
            ch_name = feed_title

        filtered_entries: List[Dict[str, Any]] = []
        discovered_dt_max: Optional[datetime] = cursor_dt

        for e in entries:
            pub_s = e.get("published_at") or ""
            pub_dt = _parse_iso_date(pub_s) if pub_s else None
            if not pub_dt:
                continue

            if not backfill:
                if cursor_dt and pub_dt <= cursor_dt:
                    continue
            else:
                end_dt = _parse_iso_date(end_date_s)
                if not end_dt:
                    continue
                if pub_dt > end_dt:
                    continue
                if start_date_s:
                    start_dt = _parse_iso_date(start_date_s)
                    if start_dt and pub_dt < start_dt:
                        continue

            if not discovered_dt_max or pub_dt > discovered_dt_max:
                discovered_dt_max = pub_dt

            vid = str(e.get("video_id") or "").strip()
            if not vid:
                continue
            if vid in done_v_ids:
                continue
            if vid in queue_video_ids:
                continue
            filtered_entries.append(e)

        if discovered_dt_max and (not last_discovered_s or discovered_dt_max > (_parse_iso_date(last_discovered_s) or datetime.min)):
            ch["last_discovered_published_at"] = discovered_dt_max.strftime("%Y-%m-%dT%H:%M:%SZ")

        if not filtered_entries:
            logger.info("channel_crawl:   accepted 0 videos after dedupe/date/output filters for cid=%s", cid)
            continue

        duration_map: Dict[str, str] = {}
        default_audio_lang_map: Dict[str, str] = {}
        if shorts_seconds_threshold > 0:
            duration_map, default_audio_lang_map = _fetch_video_durations(
                api_key, [str(x.get("video_id", "")) for x in filtered_entries]
            )

        accepted = 0
        shorts_skipped = 0
        for e in filtered_entries:
            vid = str(e.get("video_id") or "").strip()
            url = str(e.get("url") or "").strip()
            pub_s = str(e.get("published_at") or "").strip()
            duration_iso = duration_map.get(vid, "")
            default_audio_lang = default_audio_lang_map.get(vid, "")
            is_shorts = False
            if shorts_seconds_threshold > 0 and duration_iso:
                sec = _parse_duration_to_seconds(duration_iso)
                is_shorts = bool(sec is not None and sec <= shorts_seconds_threshold)
            if is_shorts:
                shorts_skipped += 1
                # Persist shorts as done in queue and also mark output_df with passed_shorts.
                discovered_rows.append({
                    "video_id": vid,
                    "url": url,
                    "channel_id": cid,
                    "channel_url": ch_url,
                    "channel_name": ch_name,
                    "usage_channel": ch_usage,
                    "published_at": pub_s,
                    "status": "done",
                    "retry_count": 0,
                    "last_error": SHORTS_STATUS,
                    "discovered_at": now_iso,
                    "last_attempted_at": now_iso,
                    "done_at": now_iso,
                    "is_shorts": "true",
                    "duration_iso": duration_iso,
                    "default_audio_lang": default_audio_lang,
                    "auto_sub_only": (ch.get("auto_sub_only") or "").strip(),
                })
                shorts_rows_for_output_df.append({
                    "date": datetime.today().strftime("%Y-%m-%d"),
                    "url": url,
                    "v_id": vid,
                    "status": SHORTS_STATUS,
                })
                queue_video_ids.add(vid)
                continue

            discovered_rows.append({
                "video_id": vid,
                "url": url,
                "channel_id": cid,
                "channel_url": ch_url,
                "channel_name": ch_name,
                "usage_channel": ch_usage,
                "published_at": pub_s,
                "status": "queued",
                "retry_count": 0,
                "last_error": "",
                "discovered_at": now_iso,
                "last_attempted_at": "",
                "done_at": "",
                "is_shorts": "false",
                "duration_iso": duration_iso,
                "default_audio_lang": default_audio_lang,
                "auto_sub_only": (ch.get("auto_sub_only") or "").strip(),
            })
            queue_video_ids.add(vid)
            accepted += 1

        logger.info(
            "channel_crawl:   accepted %d videos (shorts_skipped=%d) for cid=%s",
            accepted,
            shorts_skipped,
            cid,
        )

    # persist channel cursor updates
    save_channel_df(base_path, channels)

    if discovered_rows:
        add_df = pd.DataFrame(discovered_rows)
        queue_df = pd.concat([queue_df, add_df], ignore_index=True)

    # Final queue dedupe by video_id (keep first discovered row)
    if len(queue_df) > 0:
        queue_df["video_id"] = queue_df["video_id"].astype(str).str.strip()
        queue_df = queue_df[queue_df["video_id"] != ""]
        queue_df = queue_df.drop_duplicates(subset=["video_id"], keep="first")
        # Backfill usage_channel for rows with empty (from channel_df by channel_id)
        if "usage_channel" not in queue_df.columns:
            queue_df["usage_channel"] = ""
        cid_to_usage = {
            str(ch.get("channel_id") or "").strip(): str(ch.get("usage_channel") or "").strip()
            for ch in channels
            if str(ch.get("channel_id") or "").strip()
        }
        for idx, row in queue_df.iterrows():
            usage = str(row.get("usage_channel") or "").strip()
            cid = str(row.get("channel_id") or "").strip()
            if not usage and cid:
                u = cid_to_usage.get(cid, "")
                if u:
                    queue_df.at[idx, "usage_channel"] = u

    save_crawl_queue_df(base_path, queue_df)

    candidates_df = select_process_candidates(queue_df, max_retries)
    logger.info(
        "channel_crawl: queue persisted. total=%d, candidates=%d",
        len(queue_df),
        len(candidates_df),
    )
    if len(candidates_df) > 0:
        preview_n = min(10, len(candidates_df))
        logger.info("channel_crawl: queue candidate preview (first %d)", preview_n)
        for i in range(preview_n):
            r = candidates_df.iloc[i]
            logger.info(
                "channel_crawl:   [%d] %s | vid=%s | status=%s | retry=%s",
                i + 1,
                r.get("url", ""),
                r.get("video_id", ""),
                r.get("status", ""),
                r.get("retry_count", 0),
            )
    return queue_df, candidates_df, shorts_rows_for_output_df


def update_channel_last_processed(
    base_path: str,
    url_list: List[str],
    meta_list: List[Dict[str, Any]],
    output_df_path: str,
    success_statuses: Tuple[str, ...] = ("success", "oversized_file", "already_existed"),
) -> None:
    """
    After processing, update channel_df last_processed_published_at per channel
    using the latest published_at among successfully processed videos (by url match).
    """
    import pandas as pd
    if not url_list or not meta_list or len(url_list) != len(meta_list):
        return
    try:
        for attempt in range(3):
            try:
                out = pd.read_csv(output_df_path, encoding="utf-8-sig")
                break
            except OSError as e:
                if getattr(e, "errno", None) == 11 and attempt < 2:
                    import time
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
    except Exception as e:
        logger.warning(f"Could not read output_df for channel update: {e}")
        return
    if "url" not in out.columns or "status" not in out.columns:
        return
    url_to_meta = {m["url"]: m for m in meta_list}
    channel_max = {}
    n = len(url_list)
    tail = out.tail(n)
    if len(tail) < n:
        return
    for i, url in enumerate(url_list):
        if i >= len(tail):
            break
        row = tail.iloc[i]
        if row.get("status") not in success_statuses:
            continue
        meta = url_to_meta.get(url)
        if not meta:
            continue
        cid = meta.get("channel_id", "")
        pub = meta.get("published_at", "")
        if not cid or not pub:
            continue
        dt = _parse_iso_date(pub)
        if not dt:
            continue
        if cid not in channel_max or dt > channel_max[cid]:
            channel_max[cid] = dt

    rows = load_channel_df(base_path)
    for r in rows:
        cid = r["channel_id"]
        if cid in channel_max:
            r["last_processed_published_at"] = channel_max[cid].strftime("%Y-%m-%dT%H:%M:%SZ")
    save_channel_df(base_path, rows)
