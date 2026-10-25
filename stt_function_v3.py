# library
import os, sys, time, random, json, csv, re, html
import threading
from datetime import datetime
import pandas as pd
from tqdm import tqdm
from openai import OpenAI
import subprocess
import tiktoken
from urllib.parse import urlparse, parse_qs
import logging
from dotenv import load_dotenv

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

# Load environment variables from .env file (retry on OSError Errno 11 — often EDEADLK on macOS)
for _ in range(3):
    try:
        load_dotenv()
        break
    except OSError as e:
        if getattr(e, "errno", None) == 11 and _ < 2:
            time.sleep(2 * (_ + 1))
            continue
        raise

# Last yt-dlp / downloader failure (legacy global; prefer thread-local via get_last_ytdlp_failure_reason)
_LAST_YTDLP_FAILURE_REASON: str = ""
_download_error_local = threading.local()


def _set_last_ytdlp_failure(msg: str) -> None:
    global _LAST_YTDLP_FAILURE_REASON
    reason = (msg or "")[:8000]
    _LAST_YTDLP_FAILURE_REASON = reason
    _download_error_local.reason = reason


def get_last_ytdlp_failure_reason() -> str:
    """Last yt-dlp download error for current thread (falls back to legacy global)."""
    local = getattr(_download_error_local, "reason", None)
    if local:
        return local
    return _LAST_YTDLP_FAILURE_REASON


def clear_last_ytdlp_failure() -> None:
    _set_last_ytdlp_failure("")


def _subtitle_file_is_incomplete(path: str, video_duration_sec: float | None = None) -> bool:
    """
    Detect truncated yt-dlp subtitle writes (often ~1KiB first HTTP chunk if the process dies mid-transfer).
    """
    try:
        sz = os.path.getsize(path)
    except OSError:
        return True
    if sz <= 0:
        return True
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return True
    low = path.lower()
    if low.endswith(".vtt") and not blob.startswith(b"WEBVTT"):
        return True
    n_cues = sum(1 for line in blob.splitlines() if b"-->" in line)
    # Complete writes from yt-dlp/ffmpeg virtually always end with a newline; mid-line truncation does not.
    if not blob.endswith(b"\n"):
        return True
    # First chunk is often exactly 1024 bytes when the transfer is cut very early (seen in the wild May 2026).
    if sz <= 1100 and (video_duration_sec is None or video_duration_sec >= 90):
        return True
    if video_duration_sec is not None and video_duration_sec >= 180:
        if sz <= 4096 and n_cues < max(8, int(video_duration_sec / 120)):
            return True
    return False


# class YouTubeDownloader:
#     def __init__(self, max_daily_downloads=100):
#         self.max_daily_downloads = max_daily_downloads
#         self.download_count = 0
#         self.last_reset = datetime.now().date()
#         self.tokens = []
#         self.current_token_index = 0
        
#     def reset_daily_count(self):
#         """Reset daily download count"""
#         today = datetime.now().date()
#         if today != self.last_reset:
#             self.download_count = 0
#             self.last_reset = today
    
#     def can_download(self):
#         """Check if we can make another download"""
#         self.reset_daily_count()
#         return self.download_count < self.max_daily_downloads
    
#     def get_multiple_po_tokens(self):
#         """Generate multiple PO tokens for rotation"""
#         tokens = []
#         for _ in range(3):  # Generate 3 tokens
#             try:
#                 result = subprocess.run(
#                     ["node", "generate_token.js"],
#                     capture_output=True,
#                     text=True,
#                     check=True
#                 )
#                 token_data = json.loads(result.stdout)
#                 tokens.append(token_data)
#                 time.sleep(2)  # Wait between token generations
#             except Exception as e:
#                 print(f"Token generation error: {e}")
#         return tokens
    
#     def download_with_limits(self, URL, DOWNLOAD_PATH):
#         """Enhanced downloader with rate limiting and token rotation"""
        
#         if not self.can_download():
#             print("Daily download limit reached")
#             return None
        
#         # Generate tokens if needed
#         if not self.tokens:
#             self.tokens = self.get_multiple_po_tokens()
        
#         # Add random delay
#         time.sleep(random.uniform(2, 5))
        
#         try:
#             # Use current token in rotation
#             current_token = self.tokens[self.current_token_index % len(self.tokens)]
            
#             # Enhanced download with token rotation
#             result = self.yt_downloader_with_token_rotation(URL, DOWNLOAD_PATH, current_token)
            
#             if result:
#                 self.download_count += 1
#                 self.current_token_index = (self.current_token_index + 1) % len(self.tokens)
            
#             return result
            
#         except Exception as e:
#             print(f"Download failed: {e}")
#             return None
    
#     def set_token_environment(self, token):
#         """Set token in environment for pytubefix to use"""
#         if token and 'poToken' in token:
#             os.environ['YOUTUBE_PO_TOKEN'] = token['poToken']
#             if 'visitorData' in token:
#                 os.environ['YOUTUBE_VISITOR_DATA'] = token['visitorData']
#             print("Token set in environment")
#         else:
#             print("Invalid token format")

#     def yt_downloader_with_token_rotation(self, URL, DOWNLOAD_PATH, token):
#         """Downloader that uses specific token"""
#         try:
#             # ACTUALLY USE the token by setting it in environment
#             self.set_token_environment(token)
            
#             yt = YouTube(URL,
#                         use_po_token=True,
#                         on_progress_callback=on_progress)
            
#             # Get video info from YouTube object directly
#             video_id = yt.video_id
#             video_len = yt.length if yt.length is not None else 0
#             channel_id = yt.channel_id
#             channel_url = yt.channel_url
            
#             # Download audio
#             audio_stream = yt.streams.get_audio_only()
#             filename = sanitize_filename(audio_stream.default_filename)
            
#             audio_stream.download(output_path=DOWNLOAD_PATH, filename=filename)
#             full_saved_path = f'{DOWNLOAD_PATH}/{filename}'
            
#             print("Download and save completed")
#             return full_saved_path, filename, video_id, video_len, channel_id, channel_url
            
#         except Exception as e:
#             print(f"Error in token rotation download: {e}")
#             return None

# # Add this new function for batch processing
# def process_urls_in_batches(urls, downloader, DOWNLOAD_PATH, batch_size=10, delay_between_batches=300):
#     """Process URLs in small batches with delays"""
    
#     results = []
#     for i in range(0, len(urls), batch_size):
#         batch = urls[i:i+batch_size]
        
#         print(f"Processing batch {i//batch_size + 1} of {(len(urls) + batch_size - 1) // batch_size}")
        
#         for url in batch:
#             result = downloader.download_with_limits(url, DOWNLOAD_PATH)
#             results.append(result)
#             time.sleep(random.uniform(3, 7))  # Delay between downloads
        
#         # Longer delay between batches
#         if i + batch_size < len(urls):
#             print(f"Waiting {delay_between_batches} seconds before next batch...")
#             time.sleep(delay_between_batches)
    
#     return results

# # Add this enhanced error handling function
# def yt_downloader_robust(URL, DOWNLOAD_PATH, downloader, max_retries=3):
#     """Robust downloader with multiple retry strategies"""
    
#     for attempt in range(max_retries):
#         try:
#             result = downloader.download_with_limits(URL, DOWNLOAD_PATH)
            
#             if result:
#                 return result
                
#         except Exception as e:
#             print(f"Attempt {attempt + 1} failed: {e}")
            
#             if attempt < max_retries - 1:
#                 # Exponential backoff
#                 wait_time = (2 ** attempt) * random.uniform(5, 15)
#                 print(f"Waiting {wait_time:.1f} seconds before retry...")
#                 time.sleep(wait_time)
#             else:
#                 print("All retry attempts failed")
#                 return None
    
#     return None

# Proxy configuration from environment variable
def get_proxy_config():
    """Get proxy configuration from environment variable."""
    proxy_address = os.getenv("PROXY_ADDRESS")
    if proxy_address:
        return {
            "http": proxy_address,
            "https": proxy_address
        }
    return None

# User-Agent rotation to avoid detection
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

def get_random_user_agent():
    """Get a random User-Agent string."""
    return random.choice(USER_AGENTS)

proxy = get_proxy_config()


# v1 version
def get_youtube_po_token():
    try:
        # Run the Node.js script
        result = subprocess.run(
            ["node", "generate_token.js"],
            capture_output=True,
            text=True,
            check=True
        )
        # Parse the JSON output
        token_data = json.loads(result.stdout)
        return token_data
    except subprocess.CalledProcessError as e:
        print(f"Error: {e.stderr}")
        return None

# Call the function - 주석 처리: 모듈 import 시점 실행 방지
# token = get_youtube_po_token()

# if token:
#     print("Generated Token:", token)


# change file name-1
def sanitize_filename(filename, replacement="_", max_length=50):
    # Split the base name and the extension
    if "." in filename:
        base_name, extension = filename.rsplit(".", 1)
    else:
        base_name, extension = filename, ""

    # Sanitize the base name
    invalid_chars = r'[<>:"/\\|?*\x00-\x1F]'
    sanitized = re.sub(invalid_chars, replacement, base_name)
    sanitized_extension = re.sub(invalid_chars, replacement, extension)

    # Optionally strip leading/trailing whitespace or dots
    sanitized = sanitized.strip(" ").rstrip(".")

    # Reduce the base name length while keeping the extension
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]

    # Reassemble the sanitized name with the extension
    if extension:
        return f"{sanitized}.{sanitized_extension}"
        
    return sanitized

# change file name-2
def change_filename(filename, adding):

    if "." in filename:
        base_name, extension = filename.rsplit(".",1)
    else:
        base_name, extension = filename, ""

    # adding
    changed_name = base_name + adding
    
    if extension:
        return f"{changed_name}.{extension}" # name.extension

    else:
        print("error in file name so nothing changed")
        return filename

def change_extension(filename, new_extension):

    if "." in filename:
        base_name, old_extension = filename.rsplit(".",1)
    else:
        base_name, old_extension = filename, ""

    # change extension
    if old_extension:
        return f"{base_name}.{new_extension}" # name.new_extension

    else:
        print("error in the file name")
        return filename


_INLINE_VTT_TS_RE = re.compile(r"<\d{2}:\d{2}:\d{2}(?:[\.,]\d{1,3})?>")
_INLINE_VTT_TAG_RE = re.compile(r"</?(?:c|i|b|u|ruby|rt|v)(?:\.[^>]*)?(?:\s+[^>]*)?>", re.IGNORECASE)
_ANY_ANGLE_TAG_RE = re.compile(r"<[^>]+>")
_CUE_SETTINGS_RE = re.compile(
    r"\b(?:align|position|line|size|vertical|region):[^\s]+",
    re.IGNORECASE,
)
_NOISE_ONLY_RE = re.compile(r"^[\W_]+$")


def _normalize_spaces(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u00A0", " ").replace("\u200B", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _resolve_youtube_cookies_file(work_path=None):
    """Prefer local WORK_PATH cookies (launchd-safe); avoid Documents project path."""
    env_path = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
    if env_path and os.path.exists(env_path):
        return env_path
    candidates = []
    if work_path:
        candidates.append(os.path.join(work_path, "youtube_cookies.txt"))
    candidates.extend([
        os.path.join(os.path.dirname(__file__), "youtube_cookies.txt"),
    ])
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


def _ytdlp_js_runtime_options():
    runtime = (os.getenv("P03_YTDLP_JS_RUNTIME") or ("/opt/node-v22.23.3/bin/node" if sys.platform.startswith("linux") else "")).strip()
    return {"js_runtimes": {"node": {"path": runtime}}} if runtime else {}


def _clean_subtitle_content_line(line: str) -> str:
    """Clean one subtitle content line (keep spoken text, remove VTT markup/noise)."""
    if not line:
        return ""
    cleaned = html.unescape(line)
    cleaned = _INLINE_VTT_TS_RE.sub("", cleaned)
    cleaned = _INLINE_VTT_TAG_RE.sub("", cleaned)
    cleaned = _ANY_ANGLE_TAG_RE.sub("", cleaned)
    cleaned = _CUE_SETTINGS_RE.sub("", cleaned)
    cleaned = _normalize_spaces(cleaned)
    return cleaned


def subtitle_file_to_plain_text(path):
    """
    Read a VTT or SRT subtitle file and return plain text (strip timing/cue lines and VTT inline tags).
    Used when uploader subtitles exist so we skip Whisper and feed text to summary pipeline.
    """
    if not path or not os.path.isfile(path):
        return ""
    logger = logging.getLogger(__name__)
    text_lines = []
    total_lines = 0
    dropped_cue_lines = 0
    dropped_noise_lines = 0
    dedup_drops = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            total_lines += 1
            line = raw_line.strip()
            if not line:
                continue
            # WebVTT/SRT metadata and cue numbers
            if line.upper().startswith("WEBVTT") or line.lower().startswith("kind:") or line.lower().startswith("language:"):
                dropped_cue_lines += 1
                continue
            if re.match(r"^\d+$", line):  # SRT cue number or VTT cue id
                dropped_cue_lines += 1
                continue
            # Full timestamp line with optional cue settings: 00:00:00.000 --> 00:00:00.000 align:start position:0%
            if re.match(r"^\d{2}:\d{2}:\d{2}[\.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[\.,]\d{3}(?:\s+.*)?$", line):
                dropped_cue_lines += 1
                continue
            cleaned = _clean_subtitle_content_line(line)
            if not cleaned or _NOISE_ONLY_RE.match(cleaned):
                dropped_noise_lines += 1
                continue
            if text_lines:
                prev = text_lines[-1]
                # Immediate duplicate
                if cleaned == prev:
                    dedup_drops += 1
                    continue
                # Rolling caption growth (keep longer line)
                if cleaned.startswith(prev) and len(cleaned) > len(prev):
                    text_lines[-1] = cleaned
                    dedup_drops += 1
                    continue
                if prev.startswith(cleaned):
                    dedup_drops += 1
                    continue
            text_lines.append(cleaned)
    logger.debug(
        "subtitle cleanse: total=%d kept=%d dropped_cue=%d dropped_noise=%d dedup=%d path=%s",
        total_lines,
        len(text_lines),
        dropped_cue_lines,
        dropped_noise_lines,
        dedup_drops,
        path,
    )
    return "\n".join(text_lines).strip()


def append_video_metadata_jsonl(jsonl_path: str, upload_date: str, v_id: str,
                                transcript_date: str, method: str, md_path: str,
                                has_yid: bool = True) -> None:
    """
    Append one record to video_metadata JSONL.
    Schema: {upload_date, v_id, transcript_date, method, md_path, has_yid}
    method: "whisper" | "subs" | "auto_subs" | "no_yid"
    has_yid: True if v_id is valid, False for YID-less (method="no_yid")
    """
    record = {
        "upload_date": upload_date or "",
        "v_id": v_id or "",
        "transcript_date": transcript_date or "",
        "method": method or "",
        "md_path": md_path or "",
        "has_yid": has_yid,
    }
    try:
        parent = os.path.dirname(jsonl_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to append video_metadata JSONL: %s", e)


def _format_upload_date(info) -> str:
    """Format yt-dlp upload_date (YYYYMMDD) to YYYY-MM-DD."""
    ud = (info or {}).get("upload_date") or ""
    if len(ud) == 8:
        return f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}"
    return ud


# url extractor
def extract_youtube_id(url):

    # Parse the URL
    parsed_url = urlparse(url)
    
    # Handle 'youtu.be' short links
    if parsed_url.netloc == "youtu.be":
        
        return parsed_url.path[1:]  # Video ID is in the path
    
    # Handle 'youtube.com' links
    if parsed_url.netloc in ["www.youtube.com", "youtube.com"]:
        query_params = parse_qs(parsed_url.query)
        
        return query_params.get("v", [None])[0]  # Extract the 'v' parameter
    
    raise ValueError("Invalid YouTube URL")


def _subtitle_pick_saved_file(subs_path: str, video_id: str, langs: list) -> tuple | None:
    """
    Locate subtitle file after yt-dlp --write-subs / --write-auto-subs.
    yt-dlp may write manual captions as {id}.{lang}-orig.vtt (not only {id}.{lang}.vtt).
    """
    if not video_id or not langs:
        return None
    for lang in langs:
        for ext in (".vtt", ".srt"):
            for stem_suffix in ("", "-orig"):
                p = os.path.join(subs_path, f"{video_id}.{lang}{stem_suffix}{ext}")
                if os.path.isfile(p):
                    return (p, lang)
    for ext in (".vtt", ".srt"):
        p = os.path.join(subs_path, f"{video_id}{ext}")
        if os.path.isfile(p):
            return (p, "")
    return None


def _parse_subs_langs(subs_langs_str):
    """Parse YOUTUBE_SUBS_LANGS string to list. Default: en,ko,ja,en-US,en-GB. Maps jp->ja (YouTube uses ja)."""
    default = ["en", "ko", "ja", "en-US", "en-GB"]
    if not subs_langs_str or not isinstance(subs_langs_str, str):
        return default
    parts = [x.strip() for x in subs_langs_str.split(",") if x.strip()]
    # YouTube/yt-dlp use ISO 639-1: ja for Japanese, not jp
    normalized = ["ja" if p.lower() == "jp" else p for p in parts]
    return normalized if normalized else default


def _cleanup_ytdlp_subs_artifacts(subs_path: str, video_id: str, logger) -> None:
    """
    Before retrying after Errno 11, remove stale yt-dlp fragments (.part, .tmp) and
    zero-byte subtitle files for this video_id under subs_path.
    """
    if not subs_path or not video_id or not os.path.isdir(subs_path):
        return
    try:
        for name in os.listdir(subs_path):
            if video_id not in name:
                continue
            path = os.path.join(subs_path, name)
            if not os.path.isfile(path):
                continue
            low = name.lower()
            if low.endswith(".part") or low.endswith(".tmp") or ".part" in low or low.endswith(".ytdl"):
                try:
                    os.remove(path)
                    logger.info("Removed stale subs artifact before retry: %s", name)
                except OSError as exc:
                    logger.debug("Could not remove %s: %s", path, exc)
            elif low.endswith(".vtt") or low.endswith(".srt"):
                try:
                    if os.path.getsize(path) == 0:
                        os.remove(path)
                        logger.info("Removed zero-byte subtitle before retry: %s", name)
                except OSError:
                    pass
    except OSError as exc:
        logger.debug("cleanup_ytdlp_subs_artifacts: %s", exc)


def _resolve_primary_lang(info, prefer_lang, subs_langs):
    """
    Resolve primary language for auto-captions download (single-lang to save bandwidth).
    Priority: 1) prefer_lang if in automatic_captions, 2) first from subs_langs in ac, 3) first ac key.
    Returns language code (e.g. "en", "en-US") or None.
    """
    ac = (info or {}).get("automatic_captions") or {}
    if not isinstance(ac, dict) or not ac:
        return None
    ac_keys = list(ac.keys())

    def _match(ac_key, lang):
        if not lang:
            return False
        base = lang.split("-")[0]
        return ac_key == lang or ac_key == base or ac_key.startswith(base + "-")

    def _pick(lang):
        # Prefer the original ASR track ("ko-orig") over machine-translated variants.
        base = lang.split("-")[0]
        for k in (f"{lang}-orig", f"{base}-orig"):
            if k in ac:
                return k
        for k in ac_keys:
            if _match(k, lang):
                return k
        return None

    # 1) prefer_lang이 있고 ac에 존재
    if prefer_lang and str(prefer_lang).strip():
        k = _pick(str(prefer_lang).strip())
        if k:
            return k

    # 1b) yt-dlp가 감지한 원본 언어(info.language). 없으면 subs_langs 순서로 넘어감
    #     (예: 한국어 영상에서 'en' 번역 자막이 선택되는 문제 방지)
    detected = (info or {}).get("language")
    if detected and str(detected).strip():
        k = _pick(str(detected).strip())
        if k:
            return k

    # 2) subs_langs 순서대로 ac에 존재하는 첫 언어
    for lang in (subs_langs or []):
        for k in ac_keys:
            if _match(k, lang):
                return k

    # 3) ac의 첫 키
    return ac_keys[0] if ac_keys else None


def _yt_download_subs_only(URL, video_id, subs_path, logger, subs_langs=None, video_duration_sec=None):
    """
    Download uploader subtitles only (--write-subs --skip-download).
    Prefer VTT, then SRT. Saves to subs_path with filename {video_id}.{lang}.vtt.
    subs_langs: list of language codes (e.g. ["en","ko","ja","en-US","en-GB"]). From config YOUTUBE_SUBS_LANGS.
    video_duration_sec: optional VOD duration from extract_info (improves truncated-file detection).
    Returns (path, lang) or None on failure.
    """
    if not YT_DLP_AVAILABLE:
        return None
    try:
        os.makedirs(subs_path, exist_ok=True)
    except OSError as e:
        logger.warning("Failed to create yt_subs dir %s: %s", subs_path, e)
        return None
    langs = subs_langs if subs_langs else _parse_subs_langs(None)
    proxy_config = get_proxy_config()
    user_agent = get_random_user_agent()
    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": False,
        "subtitlesformat": "vtt/srt",
        "subtitleslangs": langs,
        "outtmpl": os.path.join(subs_path, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        # Avoid minicurses progress writing to stderr (Broken pipe under launchd log redirect).
        "noprogress": True,
        "noplaylist": True,
        # Avoid .vtt.part + final rename on macOS (reduces Errno 11 / EDEADLK on some setups)
        "nopart": True,
        "user_agent": user_agent,
        "referer": "https://www.youtube.com/",
    }
    ydl_opts.update(_ytdlp_js_runtime_options())
    if proxy_config:
        proxy_url = proxy_config.get("http") or proxy_config.get("https")
        if proxy_url:
            ydl_opts["proxy"] = proxy_url
    cookies_file = _resolve_youtube_cookies_file()
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
    max_subs_retries = 3
    for subs_attempt in range(max_subs_retries):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([URL])
            picked = _subtitle_pick_saved_file(subs_path, video_id, langs)
            if picked:
                pp, plang = picked
                psz = os.path.getsize(pp) if os.path.isfile(pp) else -1
                vd = float(video_duration_sec) if video_duration_sec else None
                if _subtitle_file_is_incomplete(pp, vd):
                    logger.warning(
                        "Discarding incomplete/truncated subtitle (%d bytes, duration_hint=%s): %s",
                        psz,
                        vd,
                        pp,
                    )
                    try:
                        os.remove(pp)
                    except OSError:
                        pass
                    _cleanup_ytdlp_subs_artifacts(subs_path, video_id, logger)
                    if subs_attempt < max_subs_retries - 1:
                        time.sleep(random.uniform(2, 6))
                    continue
                return picked
            return None
        except Exception as e:
            err_str = str(e)
            is_errno11 = getattr(e, "errno", None) == 11 or "errno 11" in err_str.lower() or "resource deadlock" in err_str.lower()
            if is_errno11 and subs_attempt < max_subs_retries - 1:
                _cleanup_ytdlp_subs_artifacts(subs_path, video_id, logger)
                wait_s = random.uniform(5, 12)
                logger.warning("Subs-only download Errno 11 for %s, retry %d/%d in %.1fs", video_id, subs_attempt + 1, max_subs_retries, wait_s)
                time.sleep(wait_s)
            else:
                logger.warning("Subs-only download failed for %s: %s", video_id, e)
                _set_last_ytdlp_failure(f"subs_only: {err_str}")
                return None
    return None


def _yt_download_auto_subs_only(URL, video_id, subs_path, logger, subs_langs=None, prefer_lang=None, info=None, video_duration_sec=None):
    """
    Download YouTube auto-generated captions only (--write-auto-subs --skip-download).
    When info is provided, downloads only the primary language (saves bandwidth).
    Prefer VTT, then SRT. Saves to subs_path with filename {video_id}.{lang}.vtt.
    subs_langs: list of language codes. From config YOUTUBE_SUBS_LANGS.
    prefer_lang: if set (e.g. from defaultAudioLanguage), used to resolve primary lang.
    info: yt-dlp extract_info result; when set, primary lang is resolved and only that lang is downloaded.
    video_duration_sec: optional VOD duration (same as extract_info duration); improves truncated-file detection.
    Returns (path, lang) or None on failure.
    """
    if not YT_DLP_AVAILABLE:
        return None
    try:
        os.makedirs(subs_path, exist_ok=True)
    except OSError as e:
        logger.warning("Failed to create yt_subs dir %s: %s", subs_path, e)
        return None
    subs_langs_list = subs_langs if subs_langs else _parse_subs_langs(None)
    primary_lang = _resolve_primary_lang(info, prefer_lang, subs_langs_list)
    if primary_lang:
        langs = [primary_lang]
        logger.debug("Auto-subs: downloading single lang %s for %s", primary_lang, video_id)
    else:
        langs = subs_langs_list
        if prefer_lang and prefer_lang not in langs:
            langs = [prefer_lang] + [x for x in langs if x != prefer_lang]
        elif prefer_lang:
            langs = [prefer_lang] + [x for x in langs if x != prefer_lang]
    proxy_config = get_proxy_config()
    user_agent = get_random_user_agent()
    ydl_opts = {
        "skip_download": True,
        "writesubtitles": False,
        "writeautomaticsub": True,
        "subtitlesformat": "vtt/srt",
        "subtitleslangs": langs,
        "outtmpl": os.path.join(subs_path, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "nopart": True,
        "user_agent": user_agent,
        "referer": "https://www.youtube.com/",
    }
    ydl_opts.update(_ytdlp_js_runtime_options())
    if proxy_config:
        proxy_url = proxy_config.get("http") or proxy_config.get("https")
        if proxy_url:
            ydl_opts["proxy"] = proxy_url
    cookies_file = _resolve_youtube_cookies_file()
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
    max_subs_retries = 3
    for subs_attempt in range(max_subs_retries):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([URL])
            picked = _subtitle_pick_saved_file(subs_path, video_id, langs)
            if picked:
                pp, plang = picked
                psz = os.path.getsize(pp) if os.path.isfile(pp) else -1
                vd = float(video_duration_sec) if video_duration_sec else None
                if info is not None and vd is None:
                    d0 = info.get("duration")
                    if d0:
                        vd = float(d0)
                if _subtitle_file_is_incomplete(pp, vd):
                    logger.warning(
                        "Discarding incomplete/truncated auto subtitle (%d bytes, duration_hint=%s): %s",
                        psz,
                        vd,
                        pp,
                    )
                    try:
                        os.remove(pp)
                    except OSError:
                        pass
                    _cleanup_ytdlp_subs_artifacts(subs_path, video_id, logger)
                    if subs_attempt < max_subs_retries - 1:
                        time.sleep(random.uniform(2, 6))
                    continue
                return picked
            return None
        except Exception as e:
            err_str = str(e)
            is_errno11 = getattr(e, "errno", None) == 11 or "errno 11" in err_str.lower() or "resource deadlock" in err_str.lower()
            if is_errno11 and subs_attempt < max_subs_retries - 1:
                _cleanup_ytdlp_subs_artifacts(subs_path, video_id, logger)
                wait_s = random.uniform(5, 12)
                logger.warning("Auto-subs download Errno 11 for %s, retry %d/%d in %.1fs", video_id, subs_attempt + 1, max_subs_retries, wait_s)
                time.sleep(wait_s)
            else:
                logger.warning("Auto-subs download failed for %s: %s", video_id, e)
                _set_last_ytdlp_failure(f"auto_subs: {err_str}")
                return None
    return None


# Caption-only entry point: audio downloads and ASR are intentionally unsupported.
def yt_downloader(URL, DOWNLOAD_PATH, ONLY_AUDIO=True, ITAG=139, max_retries=3, config=None):
    """
    Retrieve YouTube captions only through yt-dlp; never download media or run ASR.

    Args:
        URL: YouTube video URL
        DOWNLOAD_PATH: Path to save the audio file
        ONLY_AUDIO: Legacy argument, ignored by the captions-only implementation.
        ITAG: Legacy argument, ignored by the captions-only implementation.
        max_retries: Maximum number of retry attempts (default: 3)
        config: Optional caption language and workspace settings.

    Returns:
        Captions result tuple or None when no captions can be retrieved.
        subs_source: "uploader" | "auto" | None. subs_lang: language code of subs used (e.g. "en") or None. channel_name: from yt-dlp uploader/channel.
    """
    logger = logging.getLogger(__name__)
    video_id = extract_youtube_id(URL)
    if YT_DLP_AVAILABLE:
        return yt_downloader_ytdlp(URL, DOWNLOAD_PATH, video_id, max_retries, config=config)
    _set_last_ytdlp_failure("yt-dlp is required for automatic captions")
    return None

def yt_downloader_ytdlp(URL, DOWNLOAD_PATH, video_id, max_retries=3, config=None):
    """
    Retrieve YouTube captions only using yt-dlp.
    Returns 10-tuple: (audio_path, filename, video_id, video_len, channel_id, channel_url, subs_path, subs_source, subs_lang, channel_name).
    subs_source: "uploader" | "auto" | None. subs_lang: language code or None. channel_name: from yt-dlp uploader/channel.
    """
    logger = logging.getLogger(__name__)
    base_path = config.get("BASE_PATH") if config else None
    work_path = config.get("WORK_PATH") if config else None
    yt_download_if_subs_y = config.get("YT_DOWNLOAD_IF_SUBS_Y", True) if config else True
    use_auto_script = True if os.getenv("P03_DRIVE_BACKEND", "api" if sys.platform.startswith("linux") else "desktop").strip().lower() == "api" else (config.get("YOUTUBE_AUTO_SCRIPT", True) if config else True)
    subs_langs = _parse_subs_langs(config.get("YOUTUBE_SUBS_LANGS")) if config else _parse_subs_langs(None)
    _set_last_ytdlp_failure("")
    attempt = 0
    max_attempts = max_retries
    while attempt < max_attempts:
        try:
            if attempt > 0:
                wait_time = random.uniform(3, 8) * (attempt + 1)
                logger.info(f"Waiting {wait_time:.1f} seconds before retry...")
                time.sleep(wait_time)
            proxy_config = get_proxy_config()
            user_agent = get_random_user_agent()
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'noprogress': True,
                'noplaylist': True,
                'skip_download': True,
                'ignore_no_formats_error': True,
                'writesubtitles': True,
                'writeautomaticsub': True,
                'writethumbnail': False,
                'user_agent': user_agent,
                'referer': 'https://www.youtube.com/',
                'http_headers': {
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-us,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate',
                    'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.7',
                    'Connection': 'keep-alive',
                },
            }
            ydl_opts.update(_ytdlp_js_runtime_options())
            if proxy_config:
                proxy_url = proxy_config.get('http') or proxy_config.get('https')
                if proxy_url:
                    ydl_opts['proxy'] = proxy_url
            cookies_file = _resolve_youtube_cookies_file(work_path)
            if cookies_file:
                ydl_opts['cookiefile'] = cookies_file
                logger.debug(f"Using cookies file: {cookies_file}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(URL, download=False)
                # Skip only truly upcoming/live (no VOD). was_live/post_live may have VOD after processing.
                live_status = (info or {}).get("live_status") or ""
                if live_status in ("is_upcoming", "is_live"):
                    logger.info("Skipping (live_status=%s, no VOD): %s", live_status, URL)
                    return ("__LIVE_SCHEDULED__", None, video_id, None, None, None, None, None, None, "", _format_upload_date(info))
                video_len = info.get('duration', 0)
                channel_id = info.get('channel_id', '')
                channel_url = info.get('channel_url', '')
                channel_name = info.get('uploader') or info.get('channel') or ''
                title = info.get('title', f'video_{video_id}')
                auto_tracks = info.get("automatic_captions") or {}
                uploader_tracks = info.get("subtitles") or {}
                accepted_langs = [str(lang).lower() for lang in subs_langs]
                allow_uploader_subs = (os.getenv("P03_ALLOW_UPLOADER_SUBS", "").strip() == "1")
                has_auto_captions, has_uploader_captions = _caption_availability(
                    auto_tracks, uploader_tracks, accepted_langs, allow_uploader_subs)
                # Registered auto_sub_only policy means captions only; it must
                # preserve videos with either accepted uploader or automatic captions.
                auto_subs_only = bool(config.get("auto_subs_only")) if config else False
                if auto_subs_only and not (has_auto_captions or has_uploader_captions):
                    logger.info("Skipping (auto_subs_only channel, no subs): %s", URL)
                    return ("__SKIP_AUTO_SUBS_ONLY__", None, video_id, None, None, None, None, None, None, "", _format_upload_date(info))
                subs_path_result = None
                subs_source = None
                subs_lang = None
                prefer_lang = config.get("default_audio_lang") if config else None
                vd_hint = float(video_len) if video_len else None
                job_subs_dir = config.get("JOB_SUBS_DIR") if config else None
                if allow_uploader_subs and info.get("subtitles") and (base_path or work_path or job_subs_dir):
                    if job_subs_dir:
                        subs_dir = job_subs_dir
                    else:
                        subs_base = work_path if work_path else base_path
                        subs_dir = os.path.join(subs_base, "yt_subs")
                    result = _yt_download_subs_only(
                        URL, video_id, subs_dir, logger, subs_langs=subs_langs, video_duration_sec=vd_hint
                    )
                    if result is not None:
                        subs_path_result, subs_lang = result
                        subs_source = "uploader"
                    else:
                        logger.warning("Uploader subs existed but subs download failed; trying auto-captions next")
                if subs_path_result is None and use_auto_script and has_auto_captions and (base_path or work_path or job_subs_dir):
                    if job_subs_dir:
                        subs_dir = job_subs_dir
                    else:
                        subs_base = work_path if work_path else base_path
                        subs_dir = os.path.join(subs_base, "yt_subs")
                    result = _yt_download_auto_subs_only(
                        URL,
                        video_id,
                        subs_dir,
                        logger,
                        subs_langs=subs_langs,
                        prefer_lang=prefer_lang,
                        info=info,
                        video_duration_sec=vd_hint,
                    )
                    if result is not None:
                        subs_path_result, subs_lang = result
                        subs_source = "auto"
                        logger.info("YOUTUBE_AUTO_CAPTIONS_USED: True (Whisper skipped)")
                if subs_path_result is not None and subs_source == "uploader":
                    return (None, title or video_id, video_id, video_len, channel_id, channel_url, subs_path_result, subs_source, subs_lang, channel_name, _format_upload_date(info))
                if subs_path_result is not None and subs_source == "auto":
                    return (None, title or video_id, video_id, video_len, channel_id, channel_url, subs_path_result, subs_source, subs_lang, channel_name, _format_upload_date(info))
                # Production is captions-only: never fall back to media/audio/ASR.
                failure_detail = get_last_ytdlp_failure_reason().lower()
                if any(marker in failure_detail for marker in ("sign in", "private", "forbidden", "http error 403", "source unavailable")):
                    return ("__SOURCE_UNAVAILABLE__", None, video_id, video_len, channel_id, channel_url, None, None, None, channel_name, _format_upload_date(info))
                if not has_auto_captions:
                    _set_last_ytdlp_failure("NO_AUTO_CAPTION")
                    return ("__NO_AUTO_CAPTION__", None, video_id, video_len, channel_id, channel_url, None, None, None, channel_name, _format_upload_date(info))
                _set_last_ytdlp_failure("SOURCE_UNAVAILABLE: automatic caption track download failed")
                return ("__SOURCE_UNAVAILABLE__", None, video_id, video_len, channel_id, channel_url, None, None, None, channel_name, _format_upload_date(info))
        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e)
            _set_last_ytdlp_failure(error_msg)
            err_lower = error_msg.lower()
            # Member-only: skip immediately, no retry
            if "members" in err_lower and ("level" in err_lower or "only" in err_lower):
                logger.info("Skipping (member-only video): %s", URL)
                return ("__VIDEO_UNAVAILABLE__", None, video_id, None, None, None, None, None, None, "", "")
            # Private/unavailable: skip immediately (match yt-dlp phrasing e.g. "This video is private")
            if (
                "private video" in err_lower
                or "this video is private" in err_lower
                or "video is private" in err_lower
                or "video unavailable" in err_lower
                or "removed by the uploader" in err_lower
                or "removed by uploader" in err_lower
                or "this video has been removed" in err_lower
                or "video has been removed" in err_lower
                or "no longer available" in err_lower
            ):
                logger.info("Video unavailable or private (skip, no retry): %s", URL)
                return ("__VIDEO_UNAVAILABLE__", None, video_id, None, None, None, None, None, None, "", "")
            if any(marker in err_lower for marker in ("sign in to confirm", "sign in to access", "http error 403", "403 forbidden", "forbidden")):
                logger.info("Caption access refused; deferring bounded retry to next scheduled run: %s", video_id)
                return ("__SOURCE_UNAVAILABLE__", None, video_id, None, None, None, None, None, None, "", "")
            # Live event error: only skip if NOT was_live/post_live (VOD may be processing)
            if "live event" in err_lower or "this live event will begin" in err_lower:
                try:
                    live_status = (info or {}).get("live_status") or ""
                except NameError:
                    live_status = ""
                if live_status not in ("was_live", "post_live", "not_live"):
                    logger.info("Skipping (live/scheduled live event, no VOD): %s", URL)
                    return ("__LIVE_SCHEDULED__", None, video_id, None, None, None, None, None, None, "", "")
                logger.info("live_status=%s, treating as download_failed (VOD may be processing): %s", live_status, URL)
            logger.warning("yt-dlp download error for video %s (attempt %d/%d): %s", URL, attempt + 1, max_attempts, error_msg)
            
            # Check for 403 Forbidden (IP block or bot detection)
            if "403" in error_msg or "Forbidden" in error_msg:
                logger.error(f"⚠️  HTTP 403 Forbidden detected - Possible IP block or bot detection")
                logger.error(f"  Video: {URL}")
                logger.error(f"  This may indicate:")
                logger.error(f"    1. IP address has been temporarily blocked by YouTube")
                logger.error(f"    2. YouTube's bot detection is active")
                logger.error(f"    3. yt-dlp needs to be updated (run: pip install -U yt-dlp)")
                logger.error(f"  Solutions:")
                logger.error(f"    - Wait longer between requests (current wait: {wait_time if 'wait_time' in locals() else 'N/A'}s)")
                logger.error(f"    - Update yt-dlp: pip install -U yt-dlp")
                logger.error(f"    - Use cookies file (export YouTube cookies to file)")
                logger.error(f"    - Consider using a proxy/VPN")
                
                # Longer wait for 403 errors
                if attempt < max_attempts - 1:
                    wait_time = random.uniform(30, 60) * (attempt + 1)  # Much longer wait for 403
                    logger.warning(f"Waiting {wait_time:.1f} seconds before retry (longer wait for 403 error)...")
                    time.sleep(wait_time)
                    attempt += 1
                else:
                    logger.error(f"Failed to download video after {max_attempts} attempts: {URL}")
                    logger.error(f"⚠️  Consider stopping the script and waiting several hours before retrying")
                    # error_msg already stored via _set_last_ytdlp_failure
                    return None
            
            # Errno 11 on macOS is often EDEADLK ("Resource deadlock avoided"): sync/AV/rename, not only iCloud
            is_errno11 = "errno 11" in error_msg.lower() or "resource deadlock" in error_msg.lower()
            if is_errno11:
                max_attempts = max(max_attempts, max_retries + 3)
            if attempt < max_attempts - 1:
                if is_errno11:
                    wait_time = random.uniform(12, 25)
                    logger.warning(
                        "Local I/O contention (Errno 11, often EDEADLK on macOS) during yt-dlp audio download; "
                        "waiting %.1fs before retry",
                        wait_time,
                    )
                else:
                    wait_time = random.uniform(3, 8) * (attempt + 1)
                    logger.info(f"Retrying in {wait_time:.1f} seconds...")
                time.sleep(wait_time)
                attempt += 1
            else:
                logger.error(f"Failed to download video after {max_attempts} attempts: {URL}")
                # error_msg already stored via _set_last_ytdlp_failure
                return None
                
        except Exception as e:
            _set_last_ytdlp_failure(str(e))
            error_type = type(e).__name__
            logger.error(f"Unexpected error ({error_type}) while downloading with yt-dlp: {URL}, Error: {str(e)}", exc_info=True)
            if attempt < max_attempts - 1:
                wait_time = random.uniform(2, 5)
                time.sleep(wait_time)
                attempt += 1
            else:
                logger.error(f"Failed to download video after {max_attempts} attempts: {URL}")
                return None
    
    _set_last_ytdlp_failure("yt-dlp: exhausted retries without success")
    return None


def _caption_availability(auto_tracks, uploader_tracks, accepted_langs, allow_uploader):
    """Return accepted automatic/uploader caption availability from yt-dlp metadata."""
    has_auto = any(lang in auto_tracks or any(key.startswith(lang + "-") for key in auto_tracks)
                   for lang in accepted_langs)
    has_uploader = allow_uploader and any(
        lang in uploader_tracks or any(key.startswith(lang + "-") for key in uploader_tracks)
        for lang in accepted_langs)
    return has_auto, has_uploader

def _is_errno11_deadlock(exc: BaseException) -> bool:
    """macOS EDEADLK / Resource deadlock often surfaces as OSError errno 11."""
    if getattr(exc, "errno", None) == 11:
        return True
    s = str(exc).lower()
    return "errno 11" in s or "resource deadlock" in s


# record prompt log
def prompt_log(v_prompt, v_task, log_path, log_filenm = "prompt_log.json"):
    """
    Append one entry to prompt_log.json. Uses temp file + os.replace to reduce iCloud/sync Errno 11.
    Read/write retry on errno 11 (short backoff).
    """
    plog = logging.getLogger(__name__)
    os.makedirs(log_path, exist_ok=True) # ensure the directory exists

    log_file = os.path.join(log_path, log_filenm) # creating log file with json type

    logs = [] # initiate logs

    # check if the log file exists
    if os.access(log_file, os.F_OK):
        for attempt in range(4):
            try:
                with open(log_file, "r", encoding='utf-8-sig') as f:
                    logs = json.load(f)
                break
            except (json.JSONDecodeError, ValueError): # handle null json
                plog.warning("prompt_log: invalid or empty JSON in %s; starting fresh", log_file)
                logs = []
                break
            except OSError as e:
                if _is_errno11_deadlock(e) and attempt < 3:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                raise
    else:
        plog.debug("prompt_log: creating new log file %s", log_file)

    # get the timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # append the new log entry
    logs.append({"timestamp": timestamp,
                 "tasks": v_task,
                 "prompt": v_prompt})

    # Atomic write: same dir + replace (fewer iCloud partial-write issues than truncate-in-place)
    tmp_file = log_file + ".tmp"
    for attempt in range(6):
        try:
            with open(tmp_file, "w", encoding='utf-8-sig') as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, log_file)
            plog.debug("prompt_log updated: %s", timestamp)
            return
        except OSError as e:
            if _is_errno11_deadlock(e) and attempt < 5:
                time.sleep(0.5 * (attempt + 1))
                continue
            try:
                if os.path.isfile(tmp_file):
                    os.remove(tmp_file)
            except OSError:
                pass
            raise


# Input message
def prompt_engineering(INPUT_ROLE, INPUT_QUERY):

    INPUT_MESSAGE = [
        {'role': 'system',
         'content': INPUT_ROLE},
        
        {'role': 'user',
         'content': INPUT_QUERY}
    ]

    return INPUT_MESSAGE

# Input token limit for API (272k typical; use 200k to leave room for system prompt etc.)
INPUT_TOKEN_LIMIT = 200000
CHUNK_TOKEN_SIZE = 150000  # Max tokens per chunk for 2-step summarization


def count_tokens(text: str) -> int:
    """Count tokens using tiktoken (cl100k_base). Returns 0 on error."""
    if not text:
        return 0
    try:
        encoder = tiktoken.get_encoding("cl100k_base")
        return len(encoder.encode(text))
    except Exception:
        return 0


def chunk_text_by_tokens(text: str, max_tokens: int) -> list:
    """
    Split text into chunks of at most max_tokens each, trying to break at paragraph boundaries.
    Returns list of text chunks.
    """
    if not text or max_tokens <= 0:
        return [text] if text else []
    try:
        encoder = tiktoken.get_encoding("cl100k_base")
        tokens = encoder.encode(text)
    except Exception:
        return [text]
    if len(tokens) <= max_tokens:
        return [text]
    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text = encoder.decode(chunk_tokens)
        # Prefer breaking at paragraph boundary (double newline)
        if end < len(tokens) and "\n\n" in chunk_text:
            last_para = chunk_text.rfind("\n\n")
            if last_para > len(chunk_text) // 2:  # Avoid tiny chunks
                chunk_text = chunk_text[: last_para + 2]
                # Re-encode to get actual token count for next start
                try:
                    used = len(encoder.encode(chunk_text))
                    start += used
                except Exception:
                    start = end
            else:
                start = end
        else:
            start = end
        chunks.append(chunk_text)
    return chunks


# token minimizer
def token_minimizer(INPUT_ROLE, INPUT_QUERY,
                    transcription, client, model = "gpt-5-nano-2025-08-07"):

    start_time = time.time()

    FULL_PROMPT = f"""

    [Requests]
    {INPUT_QUERY}

    [Given materials]
    Please do the tasks with the following text:{transcription}
    """

    INPUT_MESSAGE = prompt_engineering(INPUT_ROLE, FULL_PROMPT)

    completion = client.chat.completions.create(
        model = model,
        messages = INPUT_MESSAGE
    )

    minimized_text = completion.choices[0].message.content

    print('Minimization complete: '+f'{time.time()-start_time:.2f} sec')
    
    return minimized_text


def token_minimizer_chunked(INPUT_ROLE, INPUT_QUERY, transcription, client,
                            model="gpt-5-nano-2025-08-07", token_limit=None,
                            skip_merge_reminimize=False):
    """
    Token minimizer with chunking for long inputs (Option B).
    If transcription exceeds token_limit, chunks it, minimizes each chunk, then combines.
    """
    limit = token_limit or INPUT_TOKEN_LIMIT
    n_tokens = count_tokens(transcription)
    if n_tokens <= limit:
        return token_minimizer(INPUT_ROLE, INPUT_QUERY, transcription, client, model)
    # Chunk and minimize each
    chunks = chunk_text_by_tokens(transcription, CHUNK_TOKEN_SIZE)
    minimized_parts = []
    for i, chunk in enumerate(chunks):
        print(f"Minimizing chunk {i + 1}/{len(chunks)} ({count_tokens(chunk)} tokens)")
        part = token_minimizer(INPUT_ROLE, INPUT_QUERY, chunk, client, model)
        minimized_parts.append(part)
    combined = "\n\n---\n\n".join(minimized_parts)
    # If combined still over limit, run one more minimization pass (optional, Phase 1c)
    if count_tokens(combined) > limit and not skip_merge_reminimize:
        print("Combined output still over limit; running final minimization pass")
        combined = token_minimizer(INPUT_ROLE, INPUT_QUERY, combined, client, model)
    elif count_tokens(combined) > limit and skip_merge_reminimize:
        print("Combined output still over limit; skipping final minimization pass (SKIP_MERGE_REMINIMIZE)")
    return combined    

# token minimizer
# def token_minimizer(INPUT_QUERY,
#                     transcription, client, model = "gpt-4.1-nano-2025-04-14"):

#     start_time = time.time()

#     FULL_PROMPT = f"""

#     [Requests]
#     {INPUT_QUERY}

#     [Given materials]
#     Please do the tasks with the following text:{transcription}
#     """

#     INPUT_MESSAGE = [
#         {'role': 'user',
#          'content': FULL_PROMPT}
#     ]

#     completion = client.chat.completions.create(
#         model = model,
#         messages = INPUT_MESSAGE
#     )

#     minimized_text = completion.choices[0].message.content

#     print('Minimization complete: '+f'{time.time()-start_time:.2f} sec')
    
#     return minimized_text

# structural prompt
def prompt_structure(transcription, filename, prompt,
                     token_range = [1.0, 1.0],
                     language = "Korean",
                     style = "Markdown"):

    # file name and transcription merge
    structured_transcription = [
        {"title": filename,
         "transcription": transcription}
    ]

    # transform into json
    json_query = json.dumps(structured_transcription, 
                            indent=2, ensure_ascii=False)

    # no. of token
    encoder = tiktoken.get_encoding("cl100k_base")

    token = encoder.encode(transcription) # count token in transcription
    # token = transcription.count(" ")
    
    print(f"Number of Token: {len(token)}")

    # input query — token/language wrapper only; content rules live in INPUT_PROMPT (main.py)
    INPUT_QUERY = f"""

    [Requirement]

    Please use the provided transcription file as {json_query}.

        1. The response should follow the {style} format.
        2. The content must be written in {language}.
        - If most of the content is not in Korean (e.g., English, Japanese, Chinese, etc.), translate it into Korean first, then summarize and reorganize it accordingly.

        3. The number of tokens in the answer should fall between {token_range[0] * len(token)} and {token_range[1] * len(token)}, depending on the quality of the content.
        - Exceeding the maximum token limit is acceptable, but the total length SHOULD not exceed more than twice the maximum length required.

        4. Do not return any meta-information about your response; provide only the answer related to the given content.

    [Request]
    {prompt}

    """

    return INPUT_QUERY


MERGE_SUMMARY_PROMPT = """
Merge section summaries into one MD. Keep order: ## 한눈에 보기 (3~5 bullets, [확정]/[정황] only), 3~6 topic ## body (2+ points each), > [!note]- Insights (2~4 bullets, [외부지식]/[추정] only; context not in video), > [!note]- Key Takeaways (3~5 so-what / risks / watch-items; do not restate 한눈에 보기), ## Tags, optional ## 용어. Callout body lines must each start with `>`. Grounding tags at bullet start only. Anti-duplication: callouts must not repeat 한눈에 보기 or 본문. Dedupe; preserve detail; one table max; no mermaid. Output document only.
"""


def _is_retryable_llm_error(exc: Exception) -> bool:
    """Return True when a different model/provider may succeed."""
    name = type(exc).__name__
    if name in (
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "APIError",
        "BadRequestError",
        "NotFoundError",
        "ServiceUnavailableError",
    ):
        return True
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = None
        if code is not None and code in (400, 404, 408, 409, 429, 500, 502, 503, 504, 529):
            return True
    msg = str(exc).lower()
    return any(
        marker in msg
        for marker in (
            "timeout",
            "timed out",
            "connection",
            "rate limit",
            "overloaded",
            "model not found",
            "does not exist",
            "not a valid model",
        )
    )


def summarize_with_chunking(transcription, filename, prompt, client,
                            token_range=(1.3, 1.5), language="Korean", style="Markdown",
                            token_limit=None, model="gpt-5-mini-2025-08-07",
                            fallback_client=None, fallback_model=None,
                            primary_provider=None, fallback_provider=None,
                            model_used_callback=None):
    """
    Summarize transcription with 2-step chunking when input exceeds token limit (Option B).
    If transcription is under limit: prompt_structure + response_text_5mini.
    If over: chunk, summarize each chunk, then merge chunk summaries.
    """
    limit = token_limit or INPUT_TOKEN_LIMIT
    llm_kwargs = {
        "fallback_client": fallback_client,
        "fallback_model": fallback_model,
        "primary_provider": primary_provider,
        "fallback_provider": fallback_provider,
        "model_used_callback": model_used_callback,
    }
    n_tokens = count_tokens(transcription)
    if n_tokens <= limit:
        full_query = prompt_structure(
            transcription, filename, prompt,
            token_range=token_range, language=language, style=style
        )
        return response_text_5mini(full_query, client, model=model, **llm_kwargs)
    # Chunk and summarize each
    chunks = chunk_text_by_tokens(transcription, CHUNK_TOKEN_SIZE)
    chunk_summaries = []
    for i, chunk in enumerate(chunks):
        print(f"Summarizing chunk {i + 1}/{len(chunks)} ({count_tokens(chunk)} tokens)")
        full_query = prompt_structure(
            chunk, filename, prompt,
            token_range=token_range, language=language, style=style
        )
        summary = response_text_5mini(full_query, client, model=model, **llm_kwargs)
        chunk_summaries.append(summary)
    combined = "\n\n---\n\n".join(chunk_summaries)
    merge_query = MERGE_SUMMARY_PROMPT + "\n\n" + combined
    return response_text_5mini(merge_query, client, model=model, **llm_kwargs)


# RESPONSE MODEL
def response_text_4o(INPUT_ROLE, INPUT_QUERY, client, model = "gpt-4o-mini-2024-07-18"):

    start_time = time.time(); print("response starts")

    INPUT_MESSAGE = prompt_engineering(INPUT_ROLE, INPUT_QUERY)

    completion = client.chat.completions.create(
        model = model,
        messages = INPUT_MESSAGE
    )

    response_text = completion.choices[0].message.content

    print('Response complete: '+f'{time.time()-start_time:.2f} sec')
    
    return response_text


def response_text_o1(INPUT_QUERY, client, model = "o1-mini-2024-09-12"):

    INPUT_MESSAGE = [
        {'role': 'user',
         'content': INPUT_QUERY}
    ]

    start_time = time.time(); print("response starts")

    completion = client.chat.completions.create(
        model = model,
        messages = INPUT_MESSAGE
    )

    response_text = completion.choices[0].message.content

    print('Response complete: '+f'{time.time()-start_time:.2f} sec')
    
    return response_text

def response_text_o3(INPUT_QUERY, client, model = "o3-mini-2025-01-31"):

    INPUT_MESSAGE = [
        {'role': 'user',
         'content': INPUT_QUERY}
    ]

    start_time = time.time(); print("response starts")

    completion = client.chat.completions.create(
        model = model,
        messages = INPUT_MESSAGE
    )

    response_text = completion.choices[0].message.content

    print('Response complete: '+f'{time.time()-start_time:.2f} sec')
    
    return response_text


def response_text_o4(INPUT_QUERY, client, model = "o4-mini-2025-04-16"):

    INPUT_MESSAGE = [
        {'role': 'user',
         'content': INPUT_QUERY}
    ]

    start_time = time.time(); print("response starts")

    completion = client.chat.completions.create(
        model = model,
        messages = INPUT_MESSAGE
    )

    response_text = completion.choices[0].message.content

    print('Response complete: '+f'{time.time()-start_time:.2f} sec')
    
    return response_text



_LLM_PRICE_PER_1M = {
    "gpt-5-mini-2025-08-07": (0.25, 2.00),
    "gpt-5-nano-2025-08-07": (0.05, 0.40),
    "openai/gpt-5-mini": (0.25, 2.00),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "deepseek/deepseek-v4-flash": (0.14, 0.28),
}


def _estimate_llm_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = _LLM_PRICE_PER_1M.get(model, (0.0, 0.0))
    return (prompt_tokens / 1_000_000) * pin + (completion_tokens / 1_000_000) * pout


def response_text_5mini(
    INPUT_QUERY,
    client,
    model="gpt-5-mini-2025-08-07",
    fallback_client=None,
    fallback_model=None,
    primary_provider=None,
    fallback_provider=None,
    model_used_callback=None,
):
    try:
        result = _response_text_5mini_once(INPUT_QUERY, client, model)
        if model_used_callback:
            model_used_callback(model)
        return result
    except Exception as exc:
        if not fallback_client or not fallback_model or not _is_retryable_llm_error(exc):
            raise
        log = logging.getLogger(__name__)
        log.warning(
            "Primary LLM failed (provider=%s model=%s): %s — retrying fallback provider=%s model=%s",
            primary_provider or "unknown",
            model,
            exc,
            fallback_provider or "unknown",
            fallback_model,
        )
        result = _response_text_5mini_once(INPUT_QUERY, fallback_client, fallback_model)
        if model_used_callback:
            model_used_callback(fallback_model)
        return result


def _response_text_5mini_once(INPUT_QUERY, client, model="gpt-5-mini-2025-08-07"):

    INPUT_MESSAGE = [
        {'role': 'user',
         'content': INPUT_QUERY}
    ]

    start_time = time.time(); print("response starts")

    completion = client.chat.completions.create(
        model = model,
        messages = INPUT_MESSAGE
    )

    response_text = completion.choices[0].message.content

    usage = getattr(completion, 'usage', None)
    if usage:
        pt = getattr(usage, 'prompt_tokens', 0) or 0
        ct = getattr(usage, 'completion_tokens', 0) or 0
        cost = _estimate_llm_cost_usd(model, pt, ct)
        logging.getLogger(__name__).info(
            "LLM usage model=%s prompt_tokens=%s completion_tokens=%s total=%s est_cost_usd=%.6f latency_sec=%.2f",
            model,
            pt,
            ct,
            pt + ct,
            cost,
            time.time() - start_time,
        )

    print('Response complete: '+f'{time.time()-start_time:.2f} sec')
    
    return response_text

def response_text_grok(INPUT_ROLE, INPUT_QUERY, client, model = "grok-2-latest"):

    start_time = time.time(); print("response starts")

    INPUT_MESSAGE = prompt_engineering(INPUT_ROLE, INPUT_QUERY)

    completion = client.chat.completions.create(
        model = model,
        messages = INPUT_MESSAGE
    )

    response_text = completion.choices[0].message.content

    print('Response complete: '+f'{time.time()-start_time:.2f} sec')
    
    return response_text


""" EXPECTED TOKEN NO. and PRICING 
def token_count():

def token_pricing():
"""
    
# end of code
