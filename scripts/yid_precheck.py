#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YID Pre-check: Count files without YID and estimate recoverability.

Scans OUTPUT_MD_PATH for MD files, counts those with/without extractable YID (video ID),
and for YID-less files estimates recoverability using Title Cache (--use-cache) or
live yt-dlp fetch. Use --build-cache first to build vid_title_cache.json.

YouTube Data API 사용 (YOUTUBE_API_KEY 필요):
  --build-cache, --write-offline-jsonl: 50개 단위 배치 조회, CPU/네트워크 부하 최소화

Usage:
  python scripts/yid_precheck.py --build-cache          # Build title cache (YouTube API)
  python scripts/yid_precheck.py --use-cache [--sample N]  # Recovery check using cache
  python scripts/yid_precheck.py --use-cache --write-offline-jsonl --sample 0  # Append YID-less to offline JSONL
  python scripts/yid_precheck.py [--sample N]           # Live fetch (yt-dlp, small samples only)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import unicodedata
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# Add project root for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

from vid_to_title_rename import (
    extract_vid_from_md_name,
    extract_vid_from_md_in_subdir,
    get_paths,
    DATE_FOLDER_PATTERN,
)
from youtube_api_metadata import fetch_video_metadata_batch, get_api_key

try:
    from config import resolve_data_root
except ImportError:
    def resolve_data_root(base_path: str, work_path=None) -> str:
        return os.path.abspath(base_path)

# Rate limiting for cache build (IP block prevention)
CACHE_MIN_DELAY = int(os.getenv("YID_CACHE_MIN_DELAY", "3"))
CACHE_MAX_DELAY = int(os.getenv("YID_CACHE_MAX_DELAY", "6"))
CACHE_EXTENDED_INTERVAL = int(os.getenv("YID_CACHE_EXTENDED_INTERVAL", "25"))
CACHE_EXTENDED_DURATION = int(os.getenv("YID_CACHE_EXTENDED_DURATION", "90"))
CACHE_MAX_RETRIES = 3
CACHE_RETRY_BASE_DELAY = 30

VID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
# +vid-VID: Whisper format, VID is present
VID_IN_WHISPER = re.compile(r"\+vid-([A-Za-z0-9_-]{11})")
DEFAULT_BASE_PATH = str(Path(__file__).resolve().parents[1])
DEFAULT_MD_PATH = ""
CACHE_FILENAME = "vid_title_cache.json"


def _get_ydl_opts():
    """Build yt-dlp opts with proxy, user-agent, cookies (IP block mitigation)."""
    try:
        import yt_dlp
    except ImportError:
        return None
    opts = {"quiet": True, "no_warnings": True, "extract_flat": False}
    proxy = os.getenv("PROXY_ADDRESS", "").strip()
    if proxy:
        opts["proxy"] = proxy
    uas = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    ]
    opts["http_headers"] = {"User-Agent": random.choice(uas)}
    for p in [
        os.getenv("YOUTUBE_COOKIES_FILE"),
        os.path.expanduser("~/youtube_cookies.txt"),
        os.path.join(os.path.dirname(__file__), "..", "youtube_cookies.txt"),
    ]:
        if p and os.path.exists(p):
            opts["cookiefile"] = p
            break
    return opts


def fetch_youtube_title_safe(video_id: str) -> str | None:
    """
    Fetch video title via yt-dlp with proxy/cookies support.
    No rate limiting (caller must add delays). Returns None on failure.
    """
    opts = _get_ydl_opts()
    if not opts:
        return None
    try:
        import yt_dlp
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("title") if info else None
    except Exception:
        return None


def fetch_youtube_upload_date_safe(video_id: str) -> str:
    """Fetch upload_date (YYYY-MM-DD) via yt-dlp. Returns '' on failure."""
    opts = _get_ydl_opts()
    if not opts:
        return ""
    try:
        import yt_dlp
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            ud = (info or {}).get("upload_date") or ""
            if len(ud) == 8:
                return f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}"
            return ud
    except Exception:
        return ""


def has_extractable_vid(name: str, in_subdir: bool = True) -> bool:
    """True if filename contains extractable VID (VID_suffix, prefix_VID_suffix, or +vid-VID)."""
    if not name.lower().endswith(".md"):
        return False
    vid, _ = extract_vid_from_md_in_subdir(name) if in_subdir else (None, None)
    if vid:
        return True
    if "+vid-" in name:
        m = VID_IN_WHISPER.search(name)
        return m is not None
    return False


def _extract_prefix_from_md_no_vid(name: str) -> str | None:
    """
    For YID-less MD filename, extract the prefix (truncated title) for matching.
    Handles: _5-mini/_auto_subs/_subs, .m4a+vid-XXX (truncated VID), plain Title.md.
    """
    if not name.lower().endswith(".md"):
        return None
    # Normalize for consistent handling (filesystem may return NFD on macOS)
    name = unicodedata.normalize("NFC", name)
    base = name[:-3]
    # +vid- with truncated VID (< 11 chars): extract prefix before +vid-
    if "+vid-" in base:
        idx = base.find("+vid-")
        prefix = base[:idx].strip("_")
        # Strip .m4a (YouTube title has no extension)
        if prefix.lower().endswith(".m4a"):
            prefix = prefix[:-4].strip("_")
        return prefix if len(prefix) >= 5 else None
    for suffix in ("_5-mini", "_auto_subs", "_subs"):
        if suffix in base:
            idx = base.find(suffix)
            return base[:idx].strip("_") if idx > 0 else ""
    # Plain truncated title
    return base.strip("_") if len(base.strip("_")) >= 5 else None


def _normalize_for_match(s: str) -> str:
    """
    Normalize string for matching: Unicode NFC, lowercase, collapse spaces.
    Handles encoding/normalization differences between filename and YouTube title.
    """
    if not s:
        return ""
    # Unicode NFC: macOS often uses NFD (decomposed), YouTube uses NFC
    s = unicodedata.normalize("NFC", s)
    # Full-width space (U+3000) -> half-width
    s = s.replace("\u3000", " ")
    # Underscore, pipe (from sanitize_filename) -> space
    s = s.replace("_", " ").replace("|", " ")
    s = re.sub(r"\s+", " ", s.lower().strip())
    return s


def _strip_trailing_jamo(s: str) -> str:
    """
    Strip trailing Hangul jamos (incomplete syllable from truncation).
    Jamo ranges: Choseong U+1100-1112, Jungseong U+1161-1175, Jongseong U+11A8-11C2.
    """
    if not s:
        return s
    while s:
        last = s[-1]
        code = ord(last)
        if (0x1100 <= code <= 0x1112) or (0x1161 <= code <= 0x1175) or (0x11A8 <= code <= 0x11C2):
            s = s[:-1]
        else:
            break
    return s


def _matches(prefix: str, yt_title: str) -> bool:
    """
    Check if prefix (truncated filename) matches YouTube title.
    Handles: Unicode NFC/NFD, trailing jamo from truncation, lenient substring match.
    """
    if not prefix or not yt_title:
        return False
    p = _normalize_for_match(prefix)
    t = _normalize_for_match(yt_title)
    if len(p) < 5:
        return False

    def _check(a: str, b: str) -> bool:
        return a in b or b.startswith(a) or (len(a) >= 10 and len(b) >= len(a) and b[: len(a) + 5].startswith(a))

    if _check(p, t):
        return True
    # Try stripping trailing jamo (truncation may have cut a syllable)
    p_stripped = _strip_trailing_jamo(p)
    if p_stripped and len(p_stripped) >= 5 and _check(p_stripped, t):
        return True
    # Try progressively shorter prefix (last char might be corrupted/partial)
    for trim in range(1, min(4, len(p) - 4)):
        p_trimmed = p[:-trim].strip()
        if len(p_trimmed) >= 5 and _check(p_trimmed, t):
            return True
    # Fallback: first 15+ chars match (handles encoding/truncation differences)
    if len(p) >= 15 and len(t) >= 15 and p[:15] == t[:15]:
        return True
    if len(p) >= 10 and len(t) >= 10 and p[:10] == t[:10] and len(p) >= len(t) * 0.5:
        return True
    return False


def _normalize_date(d: str) -> str | None:
    """Normalize date to YYYY-MM-DD. Handles 2025.1.18, 2026.3.8, 2026-03-08, etc."""
    if not d or str(d).strip().lower() in ("nan", "unknown", ""):
        return None
    s = str(d).strip()
    # 2025.1.18 or 2026.3.8
    m = re.match(r"(\d{4})[.-](\d{1,2})[.-](\d{1,2})", s)
    if m:
        y, mo, day = m.groups()
        return f"{y}-{int(mo):02d}-{int(day):02d}"
    # YYYY-MM-DD
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    return None


def build_title_cache(base_path: str, cache_path: str | None = None) -> int:
    """
    Build vid_title_cache.json from output_df success v_ids.
    Uses YouTube Data API (batch 50, 1 unit/요청). YOUTUBE_API_KEY 필요.
    Incremental: skips already-cached v_ids.
    Returns number of newly fetched titles.
    """
    api_key = get_api_key()
    if not api_key:
        print("YOUTUBE_API_KEY required in .env", file=sys.stderr)
        sys.exit(1)

    cache_path = cache_path or os.path.join(base_path, CACHE_FILENAME)
    cache: dict[str, str] = {}
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    data_root = resolve_data_root(
        base_path,
        os.getenv("WORK_PATH", "").strip() or None,
    )
    output_df = load_output_df(data_root)
    success_df = output_df[output_df["status"] == "success"]
    all_vids = success_df["v_id"].dropna().astype(str).unique().tolist()
    to_fetch = [v for v in all_vids if v not in cache and len(v) == 11]
    total = len(to_fetch)

    print(f"Title cache: {len(cache)} existing, {total} to fetch (YouTube API)")
    if total == 0:
        print("Cache complete. Nothing to fetch.")
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=0)
        return 0
    print()

    metadata_map = fetch_video_metadata_batch(api_key, to_fetch)
    for v_id, meta in metadata_map.items():
        title = meta.get("title", "")
        if title:
            cache[v_id] = title

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0)
    print(f"\nCache saved: {cache_path} ({len(cache)} entries)")
    return len(metadata_map)


def load_title_cache(base_path: str, cache_path: str | None = None) -> dict[str, str]:
    """Load vid_title_cache.json. Returns {v_id: title}."""
    cache_path = cache_path or os.path.join(base_path, CACHE_FILENAME)
    if not os.path.isfile(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_output_df(data_root: str):
    """Load output_df_new.csv from DATA_ROOT. Returns DataFrame with date, v_id, status, date_norm."""
    import pandas as pd

    path = os.path.join(data_root, "output_df_new.csv")
    if not os.path.isfile(path):
        return pd.DataFrame(columns=["date", "url", "v_id", "status"])
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
        df["date_norm"] = df["date"].astype(str).apply(_normalize_date)
        return df
    except Exception:
        return pd.DataFrame(columns=["date", "url", "v_id", "status"])


def scan_md_files(md_path: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """
    Scan MD path for files. Returns (with_yid, without_yid).
    Each item: (rel_path, transcript_date_YYYY_MM_DD, filename).
    transcript_date from folder name (YYYY_MM_DD) or None for flat files.
    """
    with_yid = []
    without_yid = []

    if not os.path.isdir(md_path):
        return with_yid, without_yid

    for entry in os.listdir(md_path):
        full = os.path.join(md_path, entry)
        if os.path.isfile(full):
            if not entry.lower().endswith(".md"):
                continue
            # Flat: YYYY-MM-DD_VID_suffix or YYYY_MM_DD_...
            date_prefix, vid, _ = extract_vid_from_md_name(entry)
            if vid or has_extractable_vid(entry, in_subdir=False):
                with_yid.append((entry, date_prefix.replace("-", "_") if date_prefix else "", entry))
            else:
                without_yid.append((entry, "", entry))
            continue
        if os.path.isdir(full):
            m = DATE_FOLDER_PATTERN.match(entry)
            if not m:
                continue
            date_folder = entry
            for f in os.listdir(full):
                if not f.lower().endswith(".md"):
                    continue
                fp = os.path.join(full, f)
                if not os.path.isfile(fp):
                    continue
                if has_extractable_vid(f, in_subdir=True):
                    with_yid.append((os.path.join(entry, f), date_folder, f))
                else:
                    without_yid.append((os.path.join(entry, f), date_folder, f))

    return with_yid, without_yid


def check_recoverability(
    without_yid: list[tuple[str, str, str]],
    output_df,
    sample: int = 20,
    verbose: bool = False,
    title_cache: dict[str, str] | None = None,
) -> tuple[int, int, list[tuple[str, str, str | None]]]:
    """
    For each YID-less file, try to match with output_df candidates.
    When title_cache is provided: use cache (no date filter, all v_ids). No network.
    When title_cache is None: use date/month fallback + live yt-dlp (rate-limited).
    Returns (recovered_count, total_checked, list of (rel_path, transcript_date, matched_v_id or None)).
    """
    import pandas as pd

    use_cache = title_cache is not None and len(title_cache) > 0

    if use_cache:
        all_vids = list(title_cache.keys())
    elif output_df is None or output_df.empty or "v_id" not in output_df.columns:
        return 0, 0, []
    else:
        success_df = output_df[output_df["status"] == "success"].copy()
        if "date_norm" not in success_df.columns:
            success_df["date_norm"] = success_df.get("date", pd.Series(dtype=object)).astype(str).apply(_normalize_date)
        all_vids = success_df["v_id"].dropna().astype(str).unique().tolist()

    to_check = without_yid if sample <= 0 else without_yid[:sample]
    results = []
    recovered = 0

    check_iter = tqdm(to_check, desc="Recovery check", unit="file") if tqdm else to_check
    for rel_path, date_folder, filename in check_iter:
        prefix = _extract_prefix_from_md_no_vid(filename)
        if not prefix:
            results.append((rel_path, date_folder, None))
            continue

        if use_cache:
            candidates = [v for v in all_vids if v in title_cache]
        else:
            candidates = all_vids[:500]  # Limit for live fetch

        if verbose:
            print(f"  [DEBUG] {rel_path}: {len(candidates)} candidates")
        if not candidates:
            results.append((rel_path, date_folder, None))
            continue

        matched = None
        for idx, v_id in enumerate(candidates):
            if not v_id or str(v_id) == "nan":
                continue
            if use_cache:
                title = title_cache.get(v_id)
            else:
                title = fetch_youtube_title_safe(v_id)
                if idx > 0 and idx % 10 == 0:
                    time.sleep(random.uniform(2, 4))
            if title and _matches(prefix, title):
                matched = str(v_id)
                recovered += 1
                break

        results.append((rel_path, date_folder, matched))

    return recovered, len(to_check), results


OFFLINE_JSONL = "video_metadata_offline.jsonl"


def _load_existing_offline_md_paths(data_root: str) -> set[str]:
    """Load md_path from video_metadata_offline.jsonl to skip duplicates."""
    path = os.path.join(data_root, OFFLINE_JSONL)
    if not os.path.isfile(path):
        return set()
    seen = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                p = rec.get("md_path", "")
                if p:
                    seen.add(os.path.normpath(p))
    except Exception:
        pass
    return seen


def _append_offline_jsonl(data_root: str, record: dict) -> None:
    """Append one record to video_metadata_offline.jsonl."""
    path = os.path.join(data_root, OFFLINE_JSONL)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_offline_jsonl_from_recovery(
    data_root: str,
    md_path: str,
    results: list[tuple[str, str, str | None]],
) -> int:
    """
    Append YID-less recovery results to video_metadata_offline.jsonl (under DATA_ROOT).
    Matched: has_yid=True, fetch upload_date via YouTube API (batch).
    No match: has_yid=False, method=no_yid.
    Returns count appended.
    """
    seen = _load_existing_offline_md_paths(data_root)
    matched_v_ids = [r[2] for r in results if r[2]]
    metadata_map = {}
    if matched_v_ids:
        api_key = get_api_key()
        if api_key:
            metadata_map = fetch_video_metadata_batch(api_key, matched_v_ids)

    appended = 0
    res_iter = tqdm(results, desc="Writing offline JSONL", unit="md") if tqdm else results
    for rel_path, date_folder, matched_v_id in res_iter:
        full_path = os.path.normpath(os.path.join(md_path, rel_path))
        if full_path in seen:
            continue
        transcript_date = date_folder.replace("_", "-") if date_folder else ""
        if matched_v_id:
            meta = metadata_map.get(matched_v_id, {})
            upload_date = meta.get("upload_date", "") or ""
            record = {
                "upload_date": upload_date,
                "v_id": matched_v_id,
                "transcript_date": transcript_date,
                "method": "offline_phase2_cache",
                "md_path": full_path,
                "has_yid": True,
            }
        else:
            record = {
                "upload_date": "",
                "v_id": "",
                "transcript_date": transcript_date,
                "method": "no_yid",
                "md_path": full_path,
                "has_yid": False,
            }
        _append_offline_jsonl(data_root, record)
        seen.add(full_path)
        appended += 1
    return appended


def main() -> None:
    parser = argparse.ArgumentParser(description="YID pre-check: count and recoverability")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--md-path", type=str, default=None)
    parser.add_argument("--sample", type=int, default=20, help="Max files to check for recovery (0=all)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print debug info (candidate counts)")
    parser.add_argument(
        "--build-cache",
        action="store_true",
        help="Build vid_title_cache.json from output_df (rate-limited, IP-block safe)",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        help="Use title cache for recovery (no live fetch, run --build-cache first)",
    )
    parser.add_argument(
        "--write-offline-jsonl",
        action="store_true",
        help="Append YID-less recovery results to video_metadata_offline.jsonl (use with --use-cache)",
    )
    args = parser.parse_args()

    base, _, _ = get_paths(args.base_path)
    base = os.path.abspath(base)
    data_root = resolve_data_root(base, os.getenv("WORK_PATH", "").strip() or None)

    if args.build_cache:
        print("=" * 60)
        print("Building Title Cache (IP-block safe)")
        print("=" * 60)
        build_title_cache(base)
        return

    md_path = args.md_path or os.getenv("OUTPUT_MD_PATH", "").strip() or DEFAULT_MD_PATH
    if not md_path:
        print("OUTPUT_MD_PATH must be set in .env or pass --md-path", file=sys.stderr)
        sys.exit(1)
    md_path = os.path.abspath(md_path)

    if not os.path.isdir(md_path):
        print(f"MD path does not exist: {md_path}", file=sys.stderr)
        sys.exit(1)

    title_cache: dict[str, str] = {}
    if args.use_cache:
        title_cache = load_title_cache(base)
        if len(title_cache) == 0:
            print("Cache empty. Run --build-cache first.", file=sys.stderr)
            sys.exit(1)

    with_yid, without_yid = scan_md_files(md_path)
    total = len(with_yid) + len(without_yid)
    n_with = len(with_yid)
    n_without = len(without_yid)

    print("=" * 60)
    print("YID Pre-check Report")
    print("=" * 60)
    print(f"MD path: {md_path}")
    print(f"Total MD files: {total}")
    print(f"With YID:      {n_with}")
    print(f"Without YID:   {n_without}")
    if total > 0:
        pct = 100 * n_without / total
        print(f"YID-less rate: {pct:.1f}%")
    print()

    if n_without == 0:
        print("No YID-less files. Reorg can proceed without recovery.")
        return

    if args.use_cache:
        print(f"Using title cache: {len(title_cache)} entries\n")

    output_df = None if args.use_cache else load_output_df(data_root)
    recovered, checked, results = check_recoverability(
        without_yid,
        output_df,
        sample=args.sample,
        verbose=args.verbose,
        title_cache=title_cache,
    )

    print("Recoverability check:" + (" (cache)" if args.use_cache else " (live yt-dlp)"))
    print(f"  Files checked: {checked}")
    print(f"  Recovered:     {recovered}")
    if checked > 0:
        rate = 100 * recovered / checked
        print(f"  Recovery rate: {rate:.1f}%")
    print()

    if results:
        print("Sample results (first 10):")
        for rel, date_f, v_id in results[:10]:
            status = f"-> {v_id}" if v_id else "no match"
            print(f"  {rel} ({date_f}) {status}")

    if args.write_offline_jsonl and args.use_cache and results:
        appended = write_offline_jsonl_from_recovery(data_root, md_path, results)
        print(f"\nAppended {appended} records to video_metadata_offline.jsonl")
        print("Run scripts/video_metadata_merge.py to merge live + offline.")

    print()
    if recovered > 0 and checked > 0:
        extrapolated = int(recovered / checked * n_without) if checked > 0 else 0
        print(f"Extrapolated recoverable (if rate holds): ~{extrapolated} of {n_without}")
    print()
    if not args.use_cache:
        print("Tip: Run --build-cache first, then --use-cache for fast full recovery (no IP block risk).")
    else:
        print("Run with --sample 0 for full recovery check.")


if __name__ == "__main__":
    main()
