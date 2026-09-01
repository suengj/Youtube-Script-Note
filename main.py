#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Speech-to-Text v4.1 - Main Script
YouTube 비디오를 다운로드하고 음성을 텍스트로 변환한 후 요약하여 마크다운으로 저장합니다.
"""

import os
import re
import sys
import subprocess
import time
import random
import logging
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple, List
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent

# .env + local scratch (TMPDIR, XDG_CACHE_HOME) before yt-dlp import via stt
load_dotenv(_PROJECT_ROOT / ".env")
try:
    os.chdir(_PROJECT_ROOT)
except OSError:
    pass  # launchd may lack Documents TCC; imports use __file__ dir via sys.path[0]
from config import (
    APP_VERSION,
    apply_work_path_scratch_env,
    get_config_dict,
    log_macos_deadlock_path_warnings,
    resolve_data_root,
)

apply_work_path_scratch_env()

from tqdm import tqdm
import pandas as pd
from openai import OpenAI

# launchd 환경에서 한글 출력을 위한 인코딩 설정
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    import io
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import stt_function_v3 as stt
import channel_crawl
import run_lock
from job_workspace import VideoJobWorkspace, cleanup_stale_jobs
from transcript_cache import (
    TranscriptCache,
    find_durable_full_transcript,
    should_write_transcript_cache,
)
from subtitle_lifecycle import (
    cleanup_expired_quarantine,
    delete_subtitle_file,
    quarantine_job_subtitles,
    cleanup_legacy_yt_subs,
)
from pipeline_models import VideoProcessResult
from pipeline_context import set_pipeline_context, clear_video_context, attach_pipeline_context_filter
from claim_manager import ClaimManager
from shared_state_writer import SharedStateWriter
from admission_limiter import DownloadAdmissionLimiter, ProviderCooldown
from preprocess_backend import create_transcript_preprocessor, TranscriptPreprocessor
from runtime_resources import device_compute_route, get_device_semaphore
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configure logging
def setup_logging(log_dir: Optional[str] = None) -> logging.Logger:
    """Setup logging configuration."""
    if log_dir is None:
        log_dir = str(_PROJECT_ROOT / "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, f"stt_{datetime.now().strftime('%Y%m%d')}.log")
    
    # Create root logger so logs from imported modules (e.g., channel_crawl) are visible.
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Remove existing handlers
    logger.handlers = []
    
    # File handler (always used)
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    # Console handler only when stdout is a real TTY (interactive). Under launchd/cron or
    # when stdout is redirected to a file (e.g. iCloud log), writing/flushing to it can
    # raise OSError [Errno 11]; skip StreamHandler so we only write to the log file.
    if sys.stdout.isatty():
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
    
    return logger

# Initialize logger
logger = setup_logging()
attach_pipeline_context_filter(logger)

# Main LLM output length target = token_range × concise input tokens
MAIN_LLM_TOKEN_RANGE = (1.5, 2.0)


@dataclass
class MainLlmConfig:
    """Primary main summarization LLM with optional fallback route."""

    primary_client: OpenAI
    primary_model: str
    primary_provider: str
    fallback_client: Optional[OpenAI] = None
    fallback_model: Optional[str] = None
    fallback_provider: Optional[str] = None

    @property
    def has_fallback(self) -> bool:
        return self.fallback_client is not None and bool(self.fallback_model)

    def summarize(
        self,
        transcription: str,
        filename: str,
        prompt: str,
        *,
        token_range=MAIN_LLM_TOKEN_RANGE,
        language: str = "Korean",
        style: str = "Markdown",
    ) -> str:
        return stt.summarize_with_chunking(
            transcription=transcription,
            filename=filename,
            prompt=prompt,
            client=self.primary_client,
            token_range=token_range,
            language=language,
            style=style,
            model=self.primary_model,
            fallback_client=self.fallback_client,
            fallback_model=self.fallback_model,
            fallback_provider=self.fallback_provider,
            primary_provider=self.primary_provider,
        )


def failure_needs_long_cooldown(status: str, error_msg: Optional[str]) -> bool:
    """
    When True, apply FAILURE_WAIT_MULTIPLIER between videos (network / rate-limit / transient I/O).
    When False, use normal MIN/MAX wait (private/unavailable/subs quirks/mlx issues, etc.).
    Transient patterns are checked first so yt-dlp messages that end with a generic suffix
    still match (e.g. timeout + "Download failed after all retry attempts").
    """
    err = (error_msg or "").lower()
    st = (status or "").lower()

    transient_markers = (
        "429",
        "rate limit",
        "too many requests",
        "quota exceeded",
        "503",
        "502",
        "504",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "internal server error",
        "timeout",
        "timed out",
        "time out",
        "connection reset",
        "reset by peer",
        "broken pipe",
        "connection aborted",
        "connection refused",
        "network is unreachable",
        "no route to host",
        "temporary failure in name resolution",
        "name or service not known",
        "errno 11",
        "resource deadlock",
        "ssl",
        "certificate verify",
        "tls handshake",
        "403",
        "forbidden",
        "bot detection",
        "unable to connect",
        "connection error",
        "try again later",
        "http error 500",
    )
    if any(m in err for m in transient_markers):
        return True

    if st == "file_error" and ("errno 11" in err or "resource deadlock" in err):
        return True

    # Content / policy / clearly non-network: short cooldown
    short_markers = (
        "private",
        "members only",
        "members-only",
        "video unavailable",
        "this video is private",
        "video is private",
        "not available in your country",
        "removed by",
        "copyright",
        "blocked in your country",
        "sign in to confirm",
        "login required",
        "invalid api key",
        "no audio stream",
        "regexmatch",
        "uploader has not made",
        "age-restricted",
        "age restricted",
        "downloaded file not found after",  # post-download mismatch (not Errno 11)
        "neither yt-dlp nor pytubefix",
        "exhausted retries without success",
    )
    if any(m in err for m in short_markers):
        return False

    # Generic message with no captured detail → short (avoid long idle)
    if err.strip() in ("", "download failed after all retry attempts"):
        return False

    # Unknown errors: prefer short cooldown (user preference: do not block batch for minutes)
    return False


LOCAL_BASE_PATH_DEFAULT = str(_PROJECT_ROOT)


def load_config() -> dict:
    """Load configuration from .env (secrets/paths) and config.py (threshold, rate limiting, channel crawl)."""
    config = {
        'BASE_PATH': os.getenv('BASE_PATH', LOCAL_BASE_PATH_DEFAULT),
        'WORK_PATH': os.getenv('WORK_PATH', '').strip() or None,
        'HF_HOME': os.getenv('HF_HOME', str(Path.home() / '.cache' / 'whisper')),
        'OUTPUT_MD_PATH': os.getenv('OUTPUT_MD_PATH', ''),
        'OUTPUT_MD_GIT': os.getenv('OUTPUT_MD_GIT', ''),
        'OPENAI_API_KEY': os.getenv('OPENAI_API_KEY'),
        'XAI_API_KEY': os.getenv('XAI_API_KEY'),
        'OPENROUTER_API_KEY': os.getenv('OPENROUTER_API_KEY', '').strip(),
        'MAIN_LLM_PROVIDER': os.getenv('MAIN_LLM_PROVIDER', 'openai').strip().lower(),
        'MAIN_LLM_MODEL': os.getenv('MAIN_LLM_MODEL', 'gpt-5-mini-2025-08-07').strip(),
        'MAIN_LLM_FALLBACK_PROVIDER': os.getenv('MAIN_LLM_FALLBACK_PROVIDER', '').strip().lower(),
        'MAIN_LLM_FALLBACK_MODEL': os.getenv('MAIN_LLM_FALLBACK_MODEL', '').strip(),
        'MAIN_LLM_OUTPUT_SUFFIX': os.getenv('MAIN_LLM_OUTPUT_SUFFIX', '5-mini').strip(),  # 5-mini=gpt-5-mini, dS4f=deepseek
        'PREPROCESS_LLM_MODEL': os.getenv('PREPROCESS_LLM_MODEL', 'gpt-5-nano-2025-08-07').strip(),
        'PROXY_ADDRESS': os.getenv('PROXY_ADDRESS', ''),
        'YOUTUBE_COOKIES_FILE': os.getenv('YOUTUBE_COOKIES_FILE', ''),
        'YOUTUBE_API_KEY': os.getenv('YOUTUBE_API_KEY', ''),
    }
    try:
        config.update(get_config_dict())
    except Exception as e:
        logger.warning(f"config.py load failed ({e}). Using defaults for rate limiting and AUDIO_SIZE_THRESHOLD_MB.")
        config.update({
            'AUDIO_SIZE_THRESHOLD_MB': 1024,
            'MIN_WAIT_BETWEEN_VIDEOS': 30,
            'MAX_WAIT_BETWEEN_VIDEOS': 60,
            'EXTENDED_WAIT_INTERVAL': 10,
            'EXTENDED_WAIT_DURATION': 300,
            'MAX_CONSECUTIVE_FAILURES': 5,
            'FAILURE_WAIT_MULTIPLIER': 2.0,
            'CHANNEL_CRAWL': False,
            'CHANNEL_BACKFILL': False,
            'CHANNEL_START_DATE': '',
            'CHANNEL_END_DATE': '',
            'FILTERING_SHORTS_MINUTES': 3,
            'CRAWL_QUEUE_MAX_RETRIES': 3,
            'YOUTUBE_AUTO_SCRIPT': True,
            'YOUTUBE_SUBS_LANGS': 'en,ko,jp,en-US,en-GB',
        })
    
    # Env overrides for YouTube subs ( .env takes precedence )
    _auto = os.getenv('YOUTUBE_AUTO_SCRIPT', '').strip().lower()
    if _auto:
        config['YOUTUBE_AUTO_SCRIPT'] = _auto in ('true', '1', 'yes')
    _langs = os.getenv('YOUTUBE_SUBS_LANGS', '').strip()
    if _langs:
        config['YOUTUBE_SUBS_LANGS'] = _langs
    _save_full = os.getenv('SAVE_FULL_WHEN_AUTO_SUBS', '').strip().lower()
    if _save_full:
        config['SAVE_FULL_WHEN_AUTO_SUBS'] = _save_full in ('true', '1', 'yes')
    _use_job = os.getenv('USE_JOB_WORKSPACE', '').strip().lower()
    if _use_job:
        config['USE_JOB_WORKSPACE'] = _use_job in ('true', '1', 'yes')
    _cache_en = os.getenv('TRANSCRIPT_CACHE_ENABLED', '').strip().lower()
    if _cache_en:
        config['TRANSCRIPT_CACHE_ENABLED'] = _cache_en in ('true', '1', 'yes')
    _cache_ttl = os.getenv('TRANSCRIPT_CACHE_TTL_HOURS', '').strip()
    if _cache_ttl.isdigit():
        config['TRANSCRIPT_CACHE_TTL_HOURS'] = int(_cache_ttl)
    _vw = os.getenv('VIDEO_WORKERS', '').strip()
    if _vw.isdigit():
        config['VIDEO_WORKERS'] = int(_vw)
    _pb = os.getenv('PREPROCESS_BACKEND', '').strip()
    if _pb:
        config['PREPROCESS_BACKEND'] = _pb
    config['VIDEO_WORKERS'] = min(2, max(1, int(config.get('VIDEO_WORKERS', 2))))
    get_device_semaphore(int(config.get('DEVICE_COMPUTE_CONCURRENCY', 1)))

    # Hot CSV / append jsonl: local DATA_ROOT when WORK_PATH or DATA_ROOT env set
    config['DATA_ROOT'] = resolve_data_root(
        config['BASE_PATH'],
        config.get('WORK_PATH'),
    )
    
    # Validate required keys
    if not config['OPENAI_API_KEY']:
        raise ValueError("OPENAI_API_KEY가 .env 파일에 설정되지 않았습니다.")
    if config.get('MAIN_LLM_PROVIDER') == 'openrouter' and not config.get('OPENROUTER_API_KEY'):
        raise ValueError("MAIN_LLM_PROVIDER=openrouter일 때 OPENROUTER_API_KEY가 .env에 필요합니다.")
    if config.get('MAIN_LLM_FALLBACK_PROVIDER') == 'openrouter' and not config.get('OPENROUTER_API_KEY'):
        raise ValueError("MAIN_LLM_FALLBACK_PROVIDER=openrouter일 때 OPENROUTER_API_KEY가 .env에 필요합니다.")
    if config.get('MAIN_LLM_FALLBACK_MODEL') and not config.get('MAIN_LLM_FALLBACK_PROVIDER'):
        raise ValueError("MAIN_LLM_FALLBACK_MODEL이 설정되면 MAIN_LLM_FALLBACK_PROVIDER도 필요합니다.")
    if config.get('CHANNEL_CRAWL') and not (config.get('YOUTUBE_API_KEY') or "").strip():
        raise ValueError("CHANNEL_CRAWL=true일 때 .env에 YOUTUBE_API_KEY가 필요합니다. docs/YOUTUBE_API_SETUP.md 참고.")
    fallback_bits = ""
    if config.get('MAIN_LLM_FALLBACK_MODEL'):
        fallback_bits = " fallback=%s (%s)" % (
            config.get('MAIN_LLM_FALLBACK_MODEL'),
            config.get('MAIN_LLM_FALLBACK_PROVIDER'),
        )
    logger.info(
        "LLM config: preprocess=%s main=%s (%s)%s output_suffix=_%s",
        config.get('PREPROCESS_LLM_MODEL'),
        config.get('MAIN_LLM_MODEL'),
        config.get('MAIN_LLM_PROVIDER'),
        fallback_bits,
        config.get('MAIN_LLM_OUTPUT_SUFFIX'),
    )
    
    # Validate paths
    paths_to_check = {
        'BASE_PATH': config['BASE_PATH'],
        'HF_HOME': config['HF_HOME'],
        'OUTPUT_MD_PATH': config['OUTPUT_MD_PATH'],
    }
    
    for key, path in paths_to_check.items():
        if not os.path.exists(path):
            logger.warning(f"{key} 경로가 존재하지 않습니다: {path}")
            logger.warning(f"경로를 생성하거나 .env 파일에서 올바른 경로로 수정해주세요.")
        else:
            logger.info(f"{key} 경로 확인됨: {path}")

    try:
        os.makedirs(config['DATA_ROOT'], exist_ok=True)
        logger.info("DATA_ROOT (input/output/channel/queue CSV, video_metadata_live.jsonl): %s", config['DATA_ROOT'])
    except OSError as e:
        logger.warning("DATA_ROOT 생성 실패 (%s): %s", config['DATA_ROOT'], e)

    log_macos_deadlock_path_warnings(
        logger,
        data_root=config.get("DATA_ROOT"),
        work_path=config.get("WORK_PATH"),
        base_path=config.get("BASE_PATH"),
        tmpdir=os.environ.get("TMPDIR", "").strip() or None,
        xdg_cache=os.environ.get("XDG_CACHE_HOME", "").strip() or None,
    )
    
    # Optional path check
    if config.get('OUTPUT_MD_GIT') and not os.path.exists(config['OUTPUT_MD_GIT']):
        logger.warning(f"OUTPUT_MD_GIT 경로가 존재하지 않습니다: {config['OUTPUT_MD_GIT']}")
        logger.warning("필요시 디렉토리를 생성하거나 .env에서 주석 처리하세요.")
    
    return config

def _create_llm_provider_client(provider: str, config: dict, openai_client: OpenAI) -> OpenAI:
    if provider == 'openrouter':
        return OpenAI(
            api_key=config['OPENROUTER_API_KEY'],
            base_url="https://openrouter.ai/api/v1",
        )
    if provider == 'openai':
        return openai_client
    raise ValueError(f"Unsupported LLM provider: {provider}")


def initialize_clients(config: dict) -> Tuple[OpenAI, Optional[OpenAI], MainLlmConfig]:
    """Initialize OpenAI (preprocess), optional XAI, and main summarization clients."""
    logger.info("Initializing API clients...")
    
    try:
        logger.info("  Initializing OpenAI client...")
        openai_client = OpenAI(api_key=config['OPENAI_API_KEY'])
        logger.info("  ✓ OpenAI client initialized")
        
        # Test API connection with a simple request (optional)
        # This can be enabled for debugging but adds overhead
        # try:
        #     models = openai_client.models.list()
        #     logger.debug(f"  OpenAI API connection test successful")
        # except Exception as e:
        #     logger.warning(f"  OpenAI API connection test failed: {str(e)}")
        
    except Exception as e:
        error_type = type(e).__name__
        logger.error(f"[ERROR] Failed to initialize OpenAI client")
        logger.error(f"  Exception Type: {error_type}")
        logger.error(f"  Error: {str(e)}")
        logger.error(f"  Possible causes: Invalid API key, network issue")
        raise
    
    xai_client = None
    if config.get('XAI_API_KEY'):
        try:
            logger.info("  Initializing XAI (Grok) client...")
            xai_client = OpenAI(api_key=config['XAI_API_KEY'], base_url="https://api.x.ai/v1")
            logger.info("  ✓ XAI client initialized")
        except Exception as e:
            error_type = type(e).__name__
            logger.warning(f"[WARNING] Failed to initialize XAI client")
            logger.warning(f"  Exception Type: {error_type}")
            logger.warning(f"  Error: {str(e)}")
            logger.warning(f"  Continuing without XAI client (optional)")
            xai_client = None
    
    primary_provider = config.get('MAIN_LLM_PROVIDER', 'openai')
    try:
        logger.info("  Initializing main LLM client (%s)...", primary_provider)
        primary_client = _create_llm_provider_client(primary_provider, config, openai_client)
        logger.info("  ✓ Main LLM primary client initialized (model=%s)", config.get('MAIN_LLM_MODEL'))
    except Exception as e:
        error_type = type(e).__name__
        logger.error("[ERROR] Failed to initialize main LLM primary client")
        logger.error("  Exception Type: %s", error_type)
        logger.error("  Error: %s", str(e))
        raise

    fallback_client = None
    fallback_provider = config.get('MAIN_LLM_FALLBACK_PROVIDER') or None
    fallback_model = config.get('MAIN_LLM_FALLBACK_MODEL') or None
    if fallback_provider and fallback_model:
        try:
            logger.info("  Initializing main LLM fallback client (%s)...", fallback_provider)
            fallback_client = _create_llm_provider_client(fallback_provider, config, openai_client)
            logger.info("  ✓ Main LLM fallback client initialized (model=%s)", fallback_model)
        except Exception as e:
            error_type = type(e).__name__
            logger.error("[ERROR] Failed to initialize main LLM fallback client")
            logger.error("  Exception Type: %s", error_type)
            logger.error("  Error: %s", str(e))
            raise

    main_llm = MainLlmConfig(
        primary_client=primary_client,
        primary_model=config.get('MAIN_LLM_MODEL', 'gpt-5-mini-2025-08-07'),
        primary_provider=primary_provider,
        fallback_client=fallback_client,
        fallback_model=fallback_model,
        fallback_provider=fallback_provider,
    )

    return openai_client, xai_client, main_llm

def load_dataframes(data_root: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Load input and output dataframes from DATA_ROOT."""
    input_df_path = os.path.join(data_root, 'input_df.csv')
    output_df_path = os.path.join(data_root, 'output_df_new.csv')
    
    try:
        if not os.path.exists(input_df_path):
            error_msg = f"Input file not found: {input_df_path}"
            logger.error(f"[ERROR] FileNotFoundError - {error_msg}")
            logger.error(f"  Expected path: {input_df_path}")
            logger.error(f"  Solution: Create input_df.csv with 'url' column")
            raise FileNotFoundError(error_msg)
        
        logger.info(f"Loading input dataframe: {input_df_path}")
        input_df = _read_csv_with_retry(input_df_path, encoding='cp949')
        
        if 'url' not in input_df.columns:
            error_msg = "Input dataframe must have 'url' column"
            logger.error(f"[ERROR] ValueError - {error_msg}")
            logger.error(f"  Columns found: {list(input_df.columns)}")
            raise ValueError(error_msg)
        
        logger.info(f"  Loaded {len(input_df)} URLs from input file")
        
    except pd.errors.EmptyDataError as e:
        logger.error(f"[ERROR] EmptyDataError - Input file is empty: {input_df_path}")
        raise
    except pd.errors.ParserError as e:
        logger.error(f"[ERROR] ParserError - Invalid CSV format: {input_df_path}")
        logger.error(f"  Error: {str(e)}")
        raise
    except UnicodeDecodeError as e:
        logger.error(f"[ERROR] UnicodeDecodeError - Encoding issue: {input_df_path}")
        logger.error(f"  Tried encoding: cp949")
        logger.error(f"  Solution: Check file encoding or convert to UTF-8")
        raise
    
    try:
        if os.path.exists(output_df_path):
            logger.info(f"Loading output dataframe: {output_df_path}")
            output_df = _read_csv_with_retry(output_df_path)
            logger.info(f"  Loaded {len(output_df)} existing records")
        else:
            logger.info(f"Output file not found, creating new: {output_df_path}")
            output_df = pd.DataFrame(columns=['date', 'url', 'v_id', 'status'])
            _save_output_df_with_retry(output_df, output_df_path, na_rep="")
            logger.info(f"  Created empty output dataframe")
        
    except Exception as e:
        error_type = type(e).__name__
        logger.error(f"[ERROR] {error_type} - Failed to load/create output dataframe")
        logger.error(f"  File path: {output_df_path}")
        logger.error(f"  Error: {str(e)}")
        raise
    
    return input_df, output_df


def _save_output_df_with_retry(output_df: pd.DataFrame, output_df_path: str, na_rep: str = "unknown", max_retries: int = 3) -> None:
    """Save output_df to CSV with retry on iCloud lock (Errno 11). Uses temp file + os.replace."""
    tmp_path = output_df_path + ".tmp"
    for attempt in range(max_retries):
        try:
            output_df.to_csv(tmp_path, na_rep=na_rep, index=False, encoding="utf-8-sig")
            os.replace(tmp_path, output_df_path)
            return
        except OSError as e:
            if getattr(e, "errno", None) == 11 and attempt < max_retries - 1:
                wait = 2 * (attempt + 1)
                logger.warning("CSV write Errno 11 (iCloud lock), retry %d/%d in %ds: %s", attempt + 1, max_retries, wait, output_df_path)
                time.sleep(wait)
                continue
            raise


def _read_csv_with_retry(path: str, encoding: str = 'utf-8-sig', max_retries: int = 6) -> pd.DataFrame:
    """Read CSV with retry on iCloud lock (Errno 11). Fallback: copy to temp then read."""
    for attempt in range(max_retries):
        try:
            return pd.read_csv(path, encoding=encoding)
        except OSError as e:
            if getattr(e, "errno", None) != 11:
                raise
            if attempt < max_retries - 1:
                wait = 3 * (attempt + 1)
                logger.warning("CSV read Errno 11 (iCloud lock), retry %d/%d in %ds: %s", attempt + 1, max_retries, wait, path)
                time.sleep(wait)
                continue
            # Last attempt failed; try reading via temp copy (avoids holding iCloud file open)
            try:
                import tempfile
                import shutil
                fd, tmp_path = tempfile.mkstemp(suffix=".csv")
                os.close(fd)
                for copy_attempt in range(3):
                    try:
                        shutil.copy2(path, tmp_path)
                        break
                    except OSError as e2:
                        if getattr(e2, "errno", None) == 11 and copy_attempt < 2:
                            time.sleep(5)
                            continue
                        raise
                try:
                    return pd.read_csv(tmp_path, encoding=encoding)
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            except Exception:
                raise


def load_output_df_only(data_root: str) -> pd.DataFrame:
    """Load or create output_df_new.csv only (for channel_crawl mode when input_df is not used)."""
    output_df_path = os.path.join(data_root, 'output_df_new.csv')
    if os.path.exists(output_df_path):
        output_df = _read_csv_with_retry(output_df_path)
        logger.info(f"Loaded output dataframe: {output_df_path} ({len(output_df)} records)")
    else:
        output_df = pd.DataFrame(columns=['date', 'url', 'v_id', 'status'])
        _save_output_df_with_retry(output_df, output_df_path, na_rep="")
        logger.info(f"Created output dataframe: {output_df_path}")
    return output_df


def get_url_list(input_df: pd.DataFrame, output_df: pd.DataFrame) -> List[str]:
    """Get list of URLs to process (excluding already processed ones)."""
    url_list = input_df['url'].tolist()
    done_urls = output_df['url'].tolist() if 'url' in output_df.columns else []
    
    url_list = list(set(url_list) - set(done_urls))
    url_list.reverse()
    
    return url_list


def get_input_urls_for_channel_crawl(data_root: str, output_df: pd.DataFrame) -> List[str]:
    """
    When CHANNEL_CRAWL=true: load input_df.csv if present and return URLs not yet in output_df.
    Returns [] if input_df.csv missing or invalid (so channel-only mode is unchanged).
    """
    input_df_path = os.path.join(data_root, 'input_df.csv')
    if not os.path.exists(input_df_path):
        return []
    try:
        input_df = _read_csv_with_retry(input_df_path, encoding='cp949')
        if input_df.empty or 'url' not in input_df.columns:
            return []
        return get_url_list(input_df, output_df)
    except Exception as e:
        logger.warning("Channel crawl: could not load input_df for merge (%s). Using channel URLs only.", e)
        return []

# Prompt templates
def build_token_query(retention_min: int, retention_max: int, *, auto_subs: bool = False) -> str:
    """Build nano minimization prompt with configurable retention (Phase 1c)."""
    filler = (
        "Remove Korean filler words (e.g. 음, 어, 그, 네, 막, 좀) and repeated phrases.\n"
        if auto_subs
        else ""
    )
    return f"""
your task is to review the given text and remove any redundant or unnecessary wording (e.g., interjections, filler words), ensuring the task is covering as possible as the full meaning and content of the original text.
{filler}
The length of the revision must not exceed the original text, but DO NOT oversimplify or excessively reduce the content.
Please, you should keep {retention_min}~{retention_max}% of the length of the original contents (excluding timestamps if exists); Do NOT miss important details in the text!

Return only the revised text in plain text format **without** adding any commentary, acknowledgments, or opinions of your own.
"""


TOKEN_QUERY = build_token_query(80, 95)

TOKEN_INPUT_ROLE = """
Please effectively corrects typos and removes unnecessary words in text
"""

PRE_TASK_TYPE = "nano_preprocess"

CONTEXT_QUERY = """
YouTube transcription → Obsidian MD. Reorganize into clear topic structure (reorder/merge OK). Mobile-scannable but substantively complete — do not skim.
"""

MOBILE_STRUCTURE_QUERY = """
Section order (exact):
1. `# {title}` — one H1.
2. `## 한눈에 보기` — 3~5 bullets; tag at start: [확정] or [정황] only (video facts; no [추정]/[외부지식]).
3. Main body — 3~6 topic `##` sections mirroring the video arc; each with 2+ concrete points (names, numbers, claims) under `##` or `###`.
4. At most one table; no mermaid/diagrams.
5. Collapsible Insights callout — every line inside must start with `>`; 2~4 bullets; tag [외부지식] or [추정] only:
   > [!note]- Insights
   > - [외부지식] context not stated in the video (framework, parallel, industry norm)
6. Collapsible Key Takeaways — every line inside must start with `>`; 3~5 bullets; so-what / risks / watch-items (optional [추정] if speculative):
   > [!note]- Key Takeaways
   > - implication for the reader — not a restatement of 한눈에 보기
7. `## Tags` — 3~5 lowercase bullets (moved to YAML).
8. Optional `## 용어` — 2~4 definitions if jargon; skip if none.
"""

CONTENT_RULES_QUERY = """
Content:
- Main body: source-only facts; prefer restructuring over deleting detail.
- No invented stats/quotes; no "부족한 점" / "개선 제안".
- Use English for transliterated terms (e.g. ChatGPT not 챗지피티).
- Anti-duplication: if a point already appears in 한눈에 보기 or 본문, do not repeat it in callouts. Callouts must add new value.
"""

INSIGHTS_GROUNDING_QUERY = """
Grounding (A4) — tag placement by section:
- 한눈에 보기 / 본문: [확정]/[정황] only; source facts.
- Insights: [외부지식] or [추정] only; general domain context OK; no invented specifics (stats, quotes, named events not in source).
- Key Takeaways: implications (risks, decisions, watch-items, investment/business hooks); synthesize across topics — do not restate 한눈에 보기.
Good Key Takeaway: "미국 의존도가 80%+이면 USMCA 재협상 리스크를 포트폴리오에 반영해야 함."
Bad Key Takeaway: "멕시코는 제조업 기회를 가졌으나 구조적 문제로 번영하지 못함." (한눈에 보기 복붙)
"""

TONE_QUERY = """
Tone: Korean, direct (avoid ~입니다/~합니다). Raw markdown only — no code fences.
"""

INPUT_PROMPT = f"""
{CONTEXT_QUERY}

{MOBILE_STRUCTURE_QUERY}

{CONTENT_RULES_QUERY}

{INSIGHTS_GROUNDING_QUERY}

{TONE_QUERY}
"""

# Legacy alias for prompt_log compatibility
INPUT_QUERY = INPUT_PROMPT

INPUT_ROLE = """
A smart assistant specialized in organizing content with expertise in analyzing and providing insights on the given material.
"""

MAIN_TASK_TYPE = "gpt_summarization"


def _build_transcript_cache(config: dict) -> TranscriptCache:
    work_path = config.get('WORK_PATH') or config.get('BASE_PATH') or str(_PROJECT_ROOT)
    cache_root = os.path.join(work_path, 'cache')
    return TranscriptCache(
        cache_root,
        enabled=bool(config.get('TRANSCRIPT_CACHE_ENABLED', True)),
        ttl_hours=int(config.get('TRANSCRIPT_CACHE_TTL_HOURS', 72)),
    )


def _run_batch_cleanup(config: dict, *, dry_run_legacy: bool = False) -> None:
    """Expire cache/quarantine/stale jobs; optional legacy yt_subs dry-run inventory."""
    work_path = config.get('WORK_PATH') or config.get('BASE_PATH') or str(_PROJECT_ROOT)
    cache = _build_transcript_cache(config)
    n_cache = cache.cleanup_expired()
    if n_cache:
        logger.info("Cleaned %d expired transcript cache entries", n_cache)
    n_q = cleanup_expired_quarantine(
        work_path,
        max_age_days=int(config.get('SUBTITLE_QUARANTINE_DAYS', 7)),
    )
    if n_q:
        logger.info("Cleaned %d expired quarantine subtitle dirs", n_q)
    max_job_age = int(config.get('STALE_JOB_MAX_AGE_HOURS', 6)) * 3600
    removed, skipped = cleanup_stale_jobs(work_path, max_age_sec=max_job_age)
    if removed or skipped:
        logger.info("Stale job cleanup: removed=%d skipped_active=%d", removed, skipped)
    if dry_run_legacy and config.get('USE_JOB_WORKSPACE', True):
        yt_subs = os.path.join(work_path, 'yt_subs')
        report = cleanup_legacy_yt_subs(
            work_path,
            yt_subs,
            os.path.join(config['BASE_PATH'], 'output_new', 'full'),
            os.path.join(config['DATA_ROOT'], 'output_df_new.csv'),
            config.get('OUTPUT_MD_PATH', ''),
            dry_run=True,
        )
        logger.info(
            "Legacy yt_subs dry-run: before=%d files (%.1f MB) deletable=%d quarantine=%d preserve=%d",
            report.before_count,
            report.before_bytes / (1024 * 1024),
            report.deleted_count,
            report.quarantined_count,
            report.preserved_count,
        )


def sanitize_channel_name(name: str) -> str:
    """Sanitize channel name for filename (filesystem-safe chars only, no length limit)."""
    if not name or not str(name).strip():
        return ""
    return re.sub(r'[/\\:*?"<>|]', '_', str(name).strip())


def _is_transient_md_write_errno(exc: BaseException) -> bool:
    """Retry macOS EDEADLK / errno 11; do not retry ENOSPC (28)."""
    n = getattr(exc, "errno", None)
    if n == 28:
        return False
    if n == 11:
        return True
    s = str(exc).lower()
    return "errno 11" in s or "resource deadlock" in s


def atomic_write_text_with_retry(
    final_path: str,
    content: str,
    *,
    encoding: str = "utf-8-sig",
    max_attempts: int = 8,
    log: Optional[logging.Logger] = None,
) -> None:
    """
    Write text via same-directory temp file + os.replace (atomic on same volume).
    Retries on transient I/O (e.g. Errno 11 on iCloud/sync paths).
    """
    lg = log or logging.getLogger(__name__)
    directory = os.path.dirname(os.path.abspath(final_path))
    os.makedirs(directory, exist_ok=True)
    base_name = os.path.basename(final_path)
    tmp_path = os.path.join(directory, f".{base_name}.{os.getpid()}.tmp")

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            with open(tmp_path, "w", encoding=encoding) as f:
                f.write(content)
            os.replace(tmp_path, final_path)
            return
        except OSError as e:
            last_exc = e
            if _is_transient_md_write_errno(e) and attempt < max_attempts - 1:
                wait = 0.45 * (attempt + 1) + random.uniform(0, 0.35)
                lg.warning(
                    "Markdown write transient I/O (attempt %d/%d), retry in %.2fs: %s",
                    attempt + 1,
                    max_attempts,
                    wait,
                    e,
                )
                time.sleep(wait)
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            if not _is_transient_md_write_errno(e) or attempt >= max_attempts - 1:
                raise
        except Exception:
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise
    if last_exc:
        raise last_exc


def process_single_video(
    v_url: str,
    config: dict,
    openai_client: OpenAI,
    main_llm: MainLlmConfig,
    output_df: pd.DataFrame,
    base_path: str,
    audio_path: str,
    output_full_path: str,
    output_smm_path: str,
    output_md_path: str,
    prompt_log_path: str,
    hf_path: str,
    *,
    run_id: str = "",
    worker_id: str = "w0",
    download_limiter: Optional[DownloadAdmissionLimiter] = None,
    provider_cooldown: Optional[ProviderCooldown] = None,
    preprocessor: Optional[TranscriptPreprocessor] = None,
) -> VideoProcessResult:
    """
    Process a single video: download, transcribe, summarize, and save.
    Does not mutate shared CSV/queue — returns VideoProcessResult for single writer.
    """
    def _vr(
        status: str,
        vid: Optional[str],
        err: Optional[str] = None,
        *,
        stage: str = "complete",
        transcript_source: Optional[str] = None,
        output_md_path_res: Optional[str] = None,
        metadata_updates: Optional[dict] = None,
        catalog_updates: Optional[dict] = None,
        prompt_entries: Optional[list] = None,
        cache_hit: bool = False,
    ) -> VideoProcessResult:
        retryable = status in {
            "download_failed", "api_error", "file_error", "mlx_error", "error",
        }
        return VideoProcessResult(
            video_id=vid or "unknown",
            source_url=v_url,
            status=status,
            stage=stage,
            retryable=retryable,
            error_message=err,
            output_md_path=output_md_path_res,
            transcript_source=transcript_source,
            transcript_cache_hit=cache_hit,
            metadata_updates=metadata_updates or {},
            catalog_updates=catalog_updates or {},
            prompt_log_entries=prompt_entries or [],
            worker_id=worker_id,
            run_id=run_id,
        )

    v_date = datetime.today().strftime("%Y-%m-%d")
    job_workspace: Optional[VideoJobWorkspace] = None
    subs_path_for_cleanup: Optional[str] = None
    pipeline_success = False
    pending_prompt_entries: list = []
    pending_metadata: dict = {}
    pending_catalog: dict = {}
    subs_path = None
    subs_source = None
    subs_lang = None
    set_pipeline_context(run_id=run_id, worker_id=worker_id, video_id="", stage="init")
    try:
        video_id = stt.extract_youtube_id(v_url)
    except (ValueError, TypeError):
        video_id = None
    error_msg = None
    error_category = None
    work_path = config.get('WORK_PATH') or base_path
    transcript_cache = _build_transcript_cache(config)

    if video_id and config.get('USE_JOB_WORKSPACE', True):
        job_workspace = VideoJobWorkspace(work_path, video_id)
        job_workspace.ensure()
        job_workspace.write_metadata({"url": v_url})
    
    try:
        # Step 1: Download YouTube video (or subs only when uploader subs exist and YT_DOWNLOAD_IF_SUBS_Y=False)
        logger.info(f"[STEP 1/5] Downloading video: {v_url}")
        set_pipeline_context(video_id=video_id or "", stage="download")
        stt.clear_last_ytdlp_failure()
        if download_limiter:
            download_limiter.wait_for_admission()
        try:
            download_config = dict(config)
            if job_workspace is not None:
                download_config['JOB_SUBS_DIR'] = job_workspace.subs_dir
            download_result = stt.yt_downloader(
                URL=v_url,
                DOWNLOAD_PATH=audio_path,
                config=download_config,
            )
            
            if download_result is None:
                error_category = "DOWNLOAD_FAILED"
                detail = stt.get_last_ytdlp_failure_reason().strip()
                error_msg = detail if detail else "Download failed after all retry attempts"
                logger.error(f"[ERROR] {error_category} - URL: {v_url}")
                logger.error(f"  Details: YouTube downloader returned None after retries — {error_msg}")
                logger.error(f"  Possible causes: Network issue, IP block, video unavailable, or YouTube API change")
                return _vr("download_failed", video_id, error_msg, stage="download")
            
            if len(download_result) >= 7 and download_result[0] == "__LIVE_SCHEDULED__":
                logger.info("[STEP 1/5] Skipped (live/scheduled live event, no VOD): %s", v_url)
                return _vr("live_scheduled", download_result[2], None, stage="download")
            if len(download_result) >= 7 and download_result[0] == "__VIDEO_UNAVAILABLE__":
                logger.info("[STEP 1/5] Skipped (video unavailable or private): %s", v_url)
                return _vr("video_unavailable", download_result[2], None, stage="download")
            if len(download_result) >= 7 and download_result[0] == "__SKIP_AUTO_SUBS_ONLY__":
                logger.info("[STEP 1/5] Skipped (auto_subs_only channel, no subs): %s", v_url)
                return _vr("skipped_auto_subs_only", download_result[2], None, stage="download")
            
            audio_path_file, audio_nm, video_id, video_len, channel_id, channel_url, subs_path, subs_source, subs_lang, channel_name_from_dl, upload_date = (
                (*download_result, None, None, None, "", "")[:11]
            )
            subs_path_for_cleanup = subs_path
            if job_workspace is not None:
                job_workspace.touch_active()
            download_triggered = audio_path_file is not None
            logger.info("DOWNLOAD_TRIGGERED: %s", download_triggered)
            if subs_path:
                if subs_source == "auto":
                    logger.info("Using YouTube auto-generated captions (Whisper skipped)")
                else:
                    logger.info("Using uploader subtitles for transcription (Whisper will be skipped)")
            logger.info(f"[STEP 1/5] ✓ Video downloaded successfully" if download_triggered else "[STEP 1/5] ✓ Subs only (no video download)")
            logger.info(f"  Video ID: {video_id}")
            logger.info(f"  Video Length: {video_len} seconds")
            logger.info(f"  Channel: {channel_id}")
            logger.info(f"  Audio file: {audio_nm}")
            
        except Exception as e:
            error_category = "DOWNLOAD_EXCEPTION"
            error_type = type(e).__name__
            error_msg = f"{error_type}: {str(e)}"
            logger.error(f"[ERROR] {error_category} - URL: {v_url}")
            logger.error(f"  Exception Type: {error_type}")
            logger.error(f"  Error Message: {str(e)}")
            logger.error(f"  Video ID extracted: {video_id if video_id else 'Failed to extract'}")
            logger.error(f"  Stack trace:", exc_info=True)
            
            # Check for specific error types
            if "RegexMatchError" in error_type:
                logger.error(f"  Cause: YouTube JavaScript structure changed (pytubefix needs update)")
            elif "VideoUnavailable" in error_type:
                logger.error(f"  Cause: Video is unavailable or private")
            elif "AgeRestrictedError" in error_type:
                logger.error(f"  Cause: Age-restricted video")
            elif "Network" in error_type or "Connection" in error_type:
                logger.error(f"  Cause: Network connectivity issue")
            
            return _vr("download_failed", video_id, error_msg, stage="download")
        
        # Step 2: Check if video already processed (retry transient failures)
        logger.info(f"[STEP 2/5] Checking if video already processed: {video_id}")
        _terminal_skip_statuses = frozenset({
            "passed_shorts", "live_scheduled", "video_unavailable",
            "skipped_auto_subs_only", "oversized_file",
        })
        _retryable_statuses = frozenset({
            "download_failed", "api_error", "file_error", "mlx_error", "error",
        })
        if 'v_id' in output_df.columns and len(output_df) > 0:
            prior = output_df[output_df['v_id'] == video_id]
            if len(prior) > 0:
                if (prior['status'] == 'success').any():
                    logger.info("[STEP 2/5] ✓ Video already processed with status: success")
                    return _vr("already_existed", video_id, None, stage="dedupe")
                latest_status = str(prior.iloc[-1]['status'])
                if latest_status in _retryable_statuses:
                    logger.info(f"[STEP 2/5] Retrying prior failed video (status: {latest_status})")
                elif latest_status == "already_existed" and (prior['status'] == 'download_failed').any():
                    logger.info(
                        "[STEP 2/5] Retrying video previously skipped after download_failed"
                    )
                elif latest_status in _terminal_skip_statuses or latest_status in (
                    "already_existed", "unknown"
                ):
                    logger.info(f"[STEP 2/5] ✓ Video already processed with status: {latest_status}")
                    return _vr("already_existed", video_id, None, stage="dedupe")
                else:
                    logger.info(f"[STEP 2/5] Retrying video with prior status: {latest_status}")
        
        # Step 3: Transcribe (uploader subs → plain text; else Whisper MLX)
        logger.info(f"[STEP 3/5] Starting transcription for video: {video_id}")
        durable_full_path: Optional[str] = None
        if subs_path:
            logger.info("WHISPER_USED: False")
            try:
                transcription = stt.subtitle_file_to_plain_text(subs_path)
                if not (transcription or "").strip():
                    raise ValueError("Empty subtitle transcription")
                # base_name from title (audio_nm when subs-only); always append video_id so VID is never omitted
                raw_base = (audio_nm or "").rsplit(".", 1)[0] if "." in (audio_nm or "") else (audio_nm or video_id)
                max_len = config.get("FILENAME_MAX_LENGTH", 50) or 0
                base_name = stt.sanitize_filename(raw_base, max_length=max_len if max_len > 0 else 99999)
                if not base_name:
                    base_name = video_id
                elif video_id and video_id not in base_name:
                    base_name = f"{base_name}_{video_id}"
                # Fallback: extract lang from subs_path (e.g. abc123.en.vtt -> en)
                if not subs_lang and subs_path:
                    bn = os.path.basename(subs_path)
                    parts = bn.rsplit(".", 2)
                    if len(parts) >= 3 and len(parts[1]) <= 5:
                        subs_lang = parts[1]
                lang_suffix = f"_{subs_lang}" if subs_lang else ""
                subs_type = "auto_subs" if subs_source == "auto" else "subs"
                txt_file_name = f"{base_name}{lang_suffix}_{subs_type}.txt"
                transcription_length = len(transcription)
                # Save full transcription to output_new/full/ (skip when auto_subs and SAVE_FULL_WHEN_AUTO_SUBS=False)
                do_save_full = (subs_source != "auto") or config.get("SAVE_FULL_WHEN_AUTO_SUBS", False)
                if do_save_full:
                    full_txt_path = os.path.join(output_full_path, txt_file_name)
                    with open(full_txt_path, "w", encoding="utf-8-sig") as f:
                        f.write(transcription)
                    logger.info(f"  Full transcription saved: {full_txt_path}")
                    durable_full_path = full_txt_path
                else:
                    durable_full_path = find_durable_full_transcript(output_full_path, video_id or "")
                if should_write_transcript_cache(
                    enabled=bool(config.get('TRANSCRIPT_CACHE_ENABLED', True)),
                    durable_full_path=durable_full_path,
                    subs_source=subs_source,
                    save_full_when_auto_subs=bool(config.get('SAVE_FULL_WHEN_AUTO_SUBS', False)),
                ):
                    transcript_cache.put(
                        video_id or "",
                        transcription,
                        transcript_source=subs_source or "subs",
                    )
                    logger.info("  Transcript cache written for %s", video_id)
                if config.get('USE_JOB_WORKSPACE', True) and subs_path:
                    if job_workspace is not None:
                        job_workspace.remove_subtitle_files()
                    else:
                        delete_subtitle_file(subs_path)
                    subs_path_for_cleanup = None
                    logger.info("  Subtitle files removed after successful parse")
                logger.info(f"[STEP 3/5] ✓ Transcription from subtitles completed")
                logger.info(f"  Subtitle file: {subs_path}")
                logger.info(f"  Transcription length: {transcription_length} characters")
            except Exception as e:
                error_category = "SUBTITLE_READ_ERROR"
                error_msg = f"{type(e).__name__}: {str(e)}"
                logger.error(f"[ERROR] {error_category} - Video ID: {video_id}")
                logger.error(f"  Subtitle path: {subs_path}")
                if config.get('USE_JOB_WORKSPACE', True) and subs_path and video_id:
                    paths = job_workspace.list_subtitle_files() if job_workspace else [subs_path]
                    quarantine_job_subtitles(work_path, video_id, paths, reason=str(e)[:200])
                return _vr("mlx_error", video_id, error_msg, stage="transcribe")
        else:
            logger.info("WHISPER_USED: True")
            logger.info(f"  Audio file path: {audio_path_file}")
            logger.info(f"  Model path: {hf_path}")
            try:
                threshold_bytes = config['AUDIO_SIZE_THRESHOLD_MB'] * 1024 * 1024
                per_video_wav = job_workspace.output_wav_path() if job_workspace else None
                with device_compute_route(label="whisper_mlx"):
                    rst = stt.transcribe_by_mlx(
                    full_load_path=audio_path_file,
                    filename=audio_nm,
                    save_path=output_full_path,
                    hf_path=hf_path,
                    base_path=base_path,
                    video_id=video_id,
                    audio_size_threshold_bytes=threshold_bytes,
                    temp_path=work_path,
                    wav_path=per_video_wav,
                )
            
                if rst is not None and isinstance(rst, tuple) and len(rst) == 2 and rst[0] is None and rst[1] == "size_threshold":
                    logger.info(f"Output WAV size >= threshold. Skipping Whisper to avoid OOM.")
                    return _vr("oversized_file", video_id, "Output WAV size exceeds threshold (oversized file)", stage="transcribe")
                if rst is None:
                    error_category = "TRANSCRIPTION_FAILED"
                    error_msg = "MLX Whisper transcription returned None"
                    logger.error(f"[ERROR] {error_category} - Video ID: {video_id}")
                    logger.error(f"  Audio file: {audio_path_file}")
                    logger.error(f"  Possible causes: Audio conversion failed, model loading failed, or transcription error")
                    logger.error(f"  Check logs for detailed error messages from transcribe_by_mlx")
                    return _vr("mlx_error", video_id, error_msg, stage="transcribe")
                
                transcription, txt_file_name = rst
                transcription_length = len(transcription)
                durable_full_path = os.path.join(output_full_path, txt_file_name)
                logger.info(f"[STEP 3/5] ✓ Transcription completed")
                logger.info(f"  Transcription file: {txt_file_name}")
                logger.info(f"  Transcription length: {transcription_length} characters")
                
            except FileNotFoundError as e:
                error_category = "TRANSCRIPTION_FILE_ERROR"
                error_msg = f"FileNotFoundError: {str(e)}"
                logger.error(f"[ERROR] {error_category} - Video ID: {video_id}")
                logger.error(f"  Missing file: {str(e)}")
                logger.error(f"  Audio file path: {audio_path_file}")
                logger.error(f"  Check if audio file exists and is accessible")
                return _vr("mlx_error", video_id, error_msg, stage="transcribe")
            except Exception as e:
                error_category = "TRANSCRIPTION_EXCEPTION"
                error_type = type(e).__name__
                error_msg = f"{error_type}: {str(e)}"
                logger.error(f"[ERROR] {error_category} - Video ID: {video_id}")
                logger.error(f"  Exception Type: {error_type}")
                logger.error(f"  Error Message: {str(e)}")
                logger.error(f"  Audio file: {audio_path_file}")
                logger.error(f"  Stack trace:", exc_info=True)
                return _vr("mlx_error", video_id, error_msg, stage="transcribe")
        
        # Step 4: Token minimization (Option B: chunked when input exceeds limit)
        logger.info(f"[STEP 4/5] Starting token minimization with %s", config.get("PREPROCESS_LLM_MODEL"))
        logger.info(f"  Input length: {transcription_length} characters")
        n_tokens = stt.count_tokens(transcription)
        if n_tokens > stt.INPUT_TOKEN_LIMIT:
            logger.info(f"  Input tokens ({n_tokens}) exceed limit; using 2-step chunked minimization")
        try:
            default_ret = config.get("NANO_RETENTION_DEFAULT") or (80, 95)
            auto_ret = config.get("NANO_RETENTION_AUTO_SUBS") or (60, 80)
            if isinstance(default_ret, (list, tuple)) and len(default_ret) >= 2:
                d_min, d_max = int(default_ret[0]), int(default_ret[1])
            else:
                d_min, d_max = 80, 95
            if isinstance(auto_ret, (list, tuple)) and len(auto_ret) >= 2:
                a_min, a_max = int(auto_ret[0]), int(auto_ret[1])
            else:
                a_min, a_max = 60, 80
            is_auto_subs = subs_source == "auto"
            r_min, r_max = (a_min, a_max) if is_auto_subs else (d_min, d_max)
            token_query = build_token_query(r_min, r_max, auto_subs=is_auto_subs)
            skip_merge = bool(config.get("SKIP_MERGE_REMINIMIZE", True))
            if is_auto_subs:
                logger.info("  Nano retention: %d~%d%% (auto_subs)", r_min, r_max)
            pre = preprocessor or create_transcript_preprocessor(
                config.get("PREPROCESS_BACKEND", "cloud_api"),
                openai_client,
            )
            set_pipeline_context(stage="preprocess", backend=config.get("PREPROCESS_BACKEND", "cloud_api"))
            concise_transcription = pre.minimize(
                TOKEN_INPUT_ROLE,
                token_query,
                transcription,
                model=config.get('PREPROCESS_LLM_MODEL', 'gpt-5-nano-2025-08-07'),
                skip_merge_reminimize=skip_merge,
            )
            concise_length = len(concise_transcription)
            reduction_rate = (1 - concise_length / transcription_length) * 100 if transcription_length > 0 else 0
            logger.info(f"[STEP 4/5] ✓ Token minimization completed")
            logger.info(f"  Output length: {concise_length} characters")
            logger.info(f"  Reduction rate: {reduction_rate:.1f}%")
            
        except Exception as e:
            error_category = "TOKEN_MINIMIZATION_ERROR"
            error_type = type(e).__name__
            error_msg = f"{error_type}: {str(e)}"
            logger.error(f"[ERROR] {error_category} - Video ID: {video_id}")
            logger.error(f"  Exception Type: {error_type}")
            logger.error(f"  Error Message: {str(e)}")
            
            # Check for API-specific errors
            if "APIError" in error_type or "RateLimitError" in error_type:
                logger.error(f"  Cause: OpenAI API error - check API key and rate limits")
            elif "AuthenticationError" in error_type:
                logger.error(f"  Cause: Invalid API key")
            elif "Timeout" in error_type:
                logger.error(f"  Cause: Request timeout - network issue")
            
            logger.error(f"  Stack trace:", exc_info=True)
            return _vr("api_error", video_id, error_msg, stage="preprocess")
        
        # Step 5: Save concise transcription
        logger.info(f"[STEP 5/5] Saving concise transcription")
        try:
            output_file = stt.change_filename(txt_file_name, f"_{config.get('MAIN_LLM_OUTPUT_SUFFIX', '5-mini')}")
            concise_file_path = os.path.join(output_smm_path, output_file)
            
            with open(concise_file_path, 'w', encoding='utf-8-sig') as f:
                f.write(concise_transcription)
            
            file_size = os.path.getsize(concise_file_path)
            logger.info(f"[STEP 5/5] ✓ Concise transcription saved")
            logger.info(f"  File: {output_file}")
            logger.info(f"  File size: {file_size} bytes")
            
        except PermissionError as e:
            error_category = "FILE_PERMISSION_ERROR"
            error_msg = f"PermissionError: {str(e)}"
            logger.error(f"[ERROR] {error_category} - Video ID: {video_id}")
            logger.error(f"  File path: {concise_file_path}")
            logger.error(f"  Cause: Insufficient permissions to write file")
            logger.error(f"  Solution: Check directory permissions")
            return _vr("file_error", video_id, error_msg, stage="save")
        except OSError as e:
            error_category = "FILE_SYSTEM_ERROR"
            error_msg = f"OSError: {str(e)}"
            logger.error(f"[ERROR] {error_category} - Video ID: {video_id}")
            logger.error(f"  File path: {concise_file_path}")
            logger.error(f"  Cause: File system error (disk full, path too long, etc.)")
            return _vr("file_error", video_id, error_msg, stage="save")
        except Exception as e:
            error_category = "FILE_SAVE_ERROR"
            error_type = type(e).__name__
            error_msg = f"{error_type}: {str(e)}"
            logger.error(f"[ERROR] {error_category} - Video ID: {video_id}")
            logger.error(f"  Exception Type: {error_type}")
            logger.error(f"  File path: {concise_file_path}")
            logger.error(f"  Stack trace:", exc_info=True)
            return _vr("file_error", video_id, error_msg, stage="save")
        
        # Step 6: defer prompt log to single writer
        pending_prompt_entries.append({
            "prompt": f'{TOKEN_QUERY}_{output_file}',
            "task": PRE_TASK_TYPE,
        })
        
        # Step 7: Generate full summary (Option B: chunked when concise exceeds limit)
        logger.info(
            "[STEP 6/7] Generating full summary with %s (%s)",
            main_llm.primary_model,
            main_llm.primary_provider,
        )
        if main_llm.has_fallback:
            logger.info(
                "  Fallback configured: %s (%s)",
                main_llm.fallback_model,
                main_llm.fallback_provider,
            )
        logger.info(f"  Input length: {concise_length} characters")
        concise_tokens = stt.count_tokens(concise_transcription)
        if concise_tokens > stt.INPUT_TOKEN_LIMIT:
            logger.info(f"  Concise tokens ({concise_tokens}) exceed limit; using 2-step chunked summarization")
        set_pipeline_context(stage="summarize", backend=main_llm.primary_provider)
        if provider_cooldown:
            provider_cooldown.wait_if_needed()
        try:
            response = main_llm.summarize(
                transcription=concise_transcription,
                filename=audio_nm,
                prompt=INPUT_PROMPT,
                token_range=list(MAIN_LLM_TOKEN_RANGE),
                language="Korean",
                style="Markdown",
            )
            response_length = len(response)
            logger.info(f"[STEP 6/7] ✓ Full summary generated")
            logger.info(f"  Response length: {response_length} characters")
            
        except Exception as e:
            error_category = "SUMMARY_GENERATION_ERROR"
            error_type = type(e).__name__
            error_msg = f"{error_type}: {str(e)}"
            if provider_cooldown and ("429" in str(e) or "RateLimit" in error_type):
                provider_cooldown.note_rate_limit()
            logger.error(f"[ERROR] {error_category} - Video ID: {video_id}")
            logger.error(f"  Exception Type: {error_type}")
            logger.error(f"  Error Message: {str(e)}")
            
            if "APIError" in error_type:
                logger.error(f"  Cause: OpenAI API error")
                logger.error(f"  Check: API key validity, rate limits, token limits")
            elif "RateLimitError" in error_type:
                logger.error(f"  Cause: Rate limit exceeded")
                logger.error(f"  Solution: Wait and retry, or upgrade API plan")
            elif "Timeout" in error_type:
                logger.error(f"  Cause: Request timeout")
                logger.error(f"  Solution: Check network connection")
            
            logger.error(f"  Stack trace:", exc_info=True)
            return _vr("api_error", video_id, error_msg, stage="preprocess")
        
        # Step 8: Save markdown (YYYY_MM_DD/채널명_파일명.md, usage_channel from channel_df)
        logger.info(f"[STEP 7/7] Saving markdown file")
        md_file_path = ""
        try:
            output_file = stt.change_filename(txt_file_name, f"_{config.get('MAIN_LLM_OUTPUT_SUFFIX', '5-mini')}")
            output_file = stt.change_extension(output_file, "md")
            # Resolve channel: usage_channel (meta) > download > fromInput|unknown
            ch_raw = config.get("usage_channel") or (channel_name_from_dl if channel_name_from_dl else "")
            if ch_raw:
                channel_prefix = sanitize_channel_name(ch_raw)
            elif config.get("from_input"):
                channel_prefix = "fromInput"
            else:
                channel_prefix = "unknown"
            if channel_prefix:
                output_file = f"{channel_prefix}_{output_file}"
            date_folder = v_date.replace("-", "_")  # 2026-01-28 -> 2026_01_28
            date_dir = os.path.join(output_md_path, date_folder)
            os.makedirs(date_dir, exist_ok=True)
            md_file_path = os.path.join(date_dir, output_file)

            # Mobile MD: YAML frontmatter + v2 body (Phase 1b)
            from scripts.md_mobile_utils import (
                assemble_mobile_md,
                build_save_entry,
                prepare_mobile_body,
            )

            body, tags, title, tldr = prepare_mobile_body(response)
            if not title:
                title = (audio_nm or "")[:120]
            suffix = config.get("MAIN_LLM_OUTPUT_SUFFIX", "5-mini")
            lang = (subs_lang or "ko").lower() if subs_lang else "ko"
            if lang == "jp":
                lang = "ja"
            ud = (upload_date[:10] if upload_date and len(upload_date) >= 10 else "")
            source_url = v_url if v_url else (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
            save_entry = build_save_entry(
                md_abs_path=md_file_path,
                md_root=output_md_path,
                vid=video_id or "",
                channel=channel_prefix,
                upload_date=ud,
                transcript_date=v_date,
                lang=lang,
                suffix=suffix,
                source_url=source_url,
                tags=tags,
                title=title,
                tldr=tldr,
            )
            content_to_write = assemble_mobile_md(save_entry, body)

            # Primary: OUTPUT_MD_PATH (e.g. Obsidian). Atomic write + retries.
            # Fallback: only if primary fails — write to WORK_PATH/output_md_mirror/ (local disk).
            work_path_cfg = (config.get("WORK_PATH") or "").strip()
            mirror_path = None
            if work_path_cfg and os.path.abspath(work_path_cfg) != os.path.abspath(base_path):
                mirror_path = os.path.join(
                    work_path_cfg, "output_md_mirror", date_folder, output_file
                )

            effective_md_path: Optional[str] = None
            try:
                atomic_write_text_with_retry(
                    md_file_path, content_to_write, encoding="utf-8-sig", log=logger
                )
                effective_md_path = md_file_path
            except Exception as primary_exc:
                logger.error(
                    "Primary markdown save failed (%s): %s",
                    md_file_path,
                    primary_exc,
                    exc_info=True,
                )
                if not mirror_path:
                    raise primary_exc
                try:
                    os.makedirs(os.path.dirname(mirror_path), exist_ok=True)
                    atomic_write_text_with_retry(
                        mirror_path, content_to_write, encoding="utf-8-sig", log=logger
                    )
                    effective_md_path = mirror_path
                    logger.warning(
                        "Markdown saved to local fallback only (Obsidian path failed): %s",
                        mirror_path,
                    )
                except Exception as fallback_exc:
                    logger.error(
                        "Local markdown fallback also failed (%s): %s",
                        mirror_path,
                        fallback_exc,
                        exc_info=True,
                    )
                    raise fallback_exc from primary_exc

            assert effective_md_path is not None
            md_file_size = os.path.getsize(effective_md_path)
            logger.info(f"[STEP 7/7] ✓ Markdown saved")
            logger.info(f"  File: {effective_md_path}")
            logger.info(f"  File size: {md_file_size} bytes")

            # Defer metadata/catalog to single writer
            method = "whisper" if not subs_path else ("subs" if subs_source == "uploader" else "auto_subs")
            jsonl_path = os.path.join(config.get("DATA_ROOT", base_path), "video_metadata_live.jsonl")
            pending_metadata = {
                "jsonl": {
                    "path": jsonl_path,
                    "upload_date": upload_date or "",
                    "video_id": video_id,
                    "transcript_date": v_date,
                    "method": method,
                    "md_path": effective_md_path,
                    "has_yid": True,
                }
            }
            wp = (config.get("WORK_PATH") or "").strip()
            dr = config.get("DATA_ROOT") or resolve_data_root(base_path, wp or None)
            pending_catalog = {
                "work_path": wp,
                "data_root": dr,
                "entry": save_entry,
            }

        except Exception as e:
            error_category = "MARKDOWN_SAVE_ERROR"
            error_type = type(e).__name__
            error_msg = f"{error_type}: {str(e)}"
            logger.error(f"[ERROR] {error_category} - Video ID: {video_id}")
            logger.error(f"  Exception Type: {error_type}")
            logger.error(f"  File path: {md_file_path or '(unknown)'}")
            logger.error(f"  Stack trace:", exc_info=True)
            return _vr("file_error", video_id, error_msg, stage="save")
        
        pending_prompt_entries.append({
            "prompt": INPUT_PROMPT,
            "task": MAIN_TASK_TYPE,
        })
        
        logger.info(f"[SUCCESS] All processes completed for video: {video_id}")
        logger.info(f"  Summary: Downloaded → Transcribed → Minimized → Summarized → Saved")
        pipeline_success = True
        ts = "whisper" if not subs_path else (subs_source or "subs")
        return _vr(
            "success",
            video_id,
            None,
            stage="complete",
            transcript_source=ts,
            output_md_path_res=effective_md_path,
            metadata_updates=pending_metadata,
            catalog_updates=pending_catalog,
            prompt_entries=pending_prompt_entries,
        )
        
    except KeyboardInterrupt:
        logger.warning(f"[INTERRUPTED] Process interrupted by user for video: {v_url}")
        logger.warning(f"  Video ID: {video_id if video_id else 'Unknown'}")
        raise  # Re-raise to be handled by main()
        
    except Exception as e:
        error_category = error_category or "UNKNOWN_ERROR"
        error_type = type(e).__name__
        error_msg = f"{error_type}: {str(e)}"
        logger.error(f"[ERROR] {error_category} - Video URL: {v_url}")
        logger.error(f"  Exception Type: {error_type}")
        logger.error(f"  Error Message: {str(e)}")
        logger.error(f"  Video ID: {video_id if video_id else 'Failed to extract'}")
        logger.error(f"  Context: Unexpected error in process_single_video")
        logger.error(f"  Stack trace:", exc_info=True)
        return _vr("error", video_id or "unknown", error_msg, stage="error")
    finally:
        clear_video_context()
        if job_workspace is not None:
            if pipeline_success:
                job_workspace.cleanup(force=True)
            else:
                job_workspace.touch_active()

def process_videos(config: dict):
    """Main function to process all videos."""
    logger.info("=" * 60)
    logger.info(f"Speech-to-Text v{APP_VERSION} - Starting video processing")
    logger.info("=" * 60)
    
    # Load configuration
    base_path = config['BASE_PATH']
    data_root = config['DATA_ROOT']
    work_path = config.get('WORK_PATH') or base_path
    if work_path != base_path:
        logger.info("WORK_PATH set: audio, yt_subs, and prompt/logs use local path (iCloud sync avoided): %s", work_path)
    if data_root != base_path:
        logger.info("DATA_ROOT set: hot CSV / crawl queue / video_metadata_live.jsonl: %s", data_root)
    hf_path = config['HF_HOME']
    logger.info(
        "Channel crawl options: FILTERING_SHORTS_MINUTES=%s, CRAWL_QUEUE_MAX_RETRIES=%s",
        config.get("FILTERING_SHORTS_MINUTES", 3),
        config.get("CRAWL_QUEUE_MAX_RETRIES", 3),
    )
    
    # Setup paths (audio + yt_subs + prompt logs on work_path when set, to avoid iCloud Errno 11 on writes)
    audio_path = os.path.join(work_path, 'audio')
    prompt_path = (
        os.path.join(work_path, 'prompt')
        if work_path != base_path
        else os.path.join(base_path, 'prompt')
    )
    output_full_path = os.path.join(base_path, 'output_new', 'full')
    output_smm_path = os.path.join(base_path, 'output_new', 'summary')
    output_md_path = config['OUTPUT_MD_PATH']
    prompt_log_path = os.path.join(prompt_path, 'logs')
    output_df_path = os.path.join(data_root, 'output_df_new.csv')
    
    # Create directories
    yt_subs_path = os.path.join(work_path, 'yt_subs')
    directories_to_create = [audio_path, output_full_path, output_smm_path, output_md_path, prompt_log_path, yt_subs_path]
    
    # Add optional OUTPUT_MD_GIT if configured
    if config.get('OUTPUT_MD_GIT'):
        directories_to_create.append(config['OUTPUT_MD_GIT'])
    
    for path in directories_to_create:
        try:
            os.makedirs(path, exist_ok=True)
            logger.debug(f"Directory ready: {path}")
        except Exception as e:
            logger.error(f"Failed to create directory {path}: {str(e)}")
            raise
    
    # Initialize clients
    openai_client, xai_client, main_llm = initialize_clients(config)
    
    url_list = []
    meta_for_channel_crawl = []
    url_from_input_set = set()
    crawl_queue_df = None
    shorts_recorded_count = 0
    if config.get('CHANNEL_CRAWL'):
        output_df = load_output_df_only(data_root)
        logger.info("Channel crawl: building persistent queue from channel_df.csv...")
        try:
            crawl_queue_df, candidate_df, shorts_rows = channel_crawl.build_queue_and_get_candidates(data_root, config, output_df)
            if shorts_rows:
                shorts_df = pd.DataFrame(shorts_rows)
                if len(shorts_df) > 0:
                    # Avoid duplicate append for the same video_id already in output_df.
                    existing_v_ids = set(output_df["v_id"].astype(str).str.strip()) if "v_id" in output_df.columns else set()
                    shorts_df["v_id"] = shorts_df["v_id"].astype(str).str.strip()
                    shorts_df = shorts_df[~shorts_df["v_id"].isin(existing_v_ids)]
                    if len(shorts_df) > 0:
                        shorts_recorded_count = len(shorts_df)
                        output_df = pd.concat([output_df, shorts_df], ignore_index=True)
                        _save_output_df_with_retry(output_df, output_df_path)
                        logger.info(
                            "Channel crawl: recorded %d shorts as status=%s to output_df",
                            len(shorts_df),
                            channel_crawl.SHORTS_STATUS,
                        )
            if candidate_df is not None and len(candidate_df) > 0:
                url_list = candidate_df["url"].astype(str).tolist()
                meta_for_channel_crawl = []
                url_from_input_set = set()
                for _, r in candidate_df.iterrows():
                    meta_for_channel_crawl.append({
                        "url": str(r.get("url", "")),
                        "published_at": str(r.get("published_at", "")),
                        "channel_id": str(r.get("channel_id", "")),
                        "usage_channel": str(r.get("usage_channel", "")).strip(),
                        "video_id": str(r.get("video_id", "")),
                        "default_audio_lang": str(r.get("default_audio_lang", "")),
                        "auto_subs_only": bool((str(r.get("auto_sub_only", "")).strip())),
                    })
                # Merge input_df URLs (not yet in output_df) so both channel crawl and manual input are processed
                input_urls = get_input_urls_for_channel_crawl(data_root, output_df)
                crawl_set = set(url_list)
                input_only = [u for u in input_urls if u not in crawl_set]
                if input_only:
                    url_list = url_list + input_only
                    url_from_input_set = set(input_only)
                    logger.info("Channel crawl: added %d URL(s) from input_df.csv (total queue: %d)", len(input_only), len(url_list))
                else:
                    url_from_input_set = set()
            else:
                url_list = []
                meta_for_channel_crawl = []
                url_from_input_set = set()
            # When no channel candidates, still process input_df if present
            if not url_list:
                input_urls = get_input_urls_for_channel_crawl(data_root, output_df)
                if input_urls:
                    url_list = input_urls
                    url_from_input_set = set(url_list)
                    logger.info("Channel crawl: no channel candidates; using %d URL(s) from input_df.csv", len(url_list))
        except ValueError as e:
            # ValueError/UnicodeError 계열 메시지 출력
            error_msg = str(e) if e else "Configuration error"
            logger.error("[ERROR] Channel crawl config: %s", error_msg)
            return
        logger.info(f"Channel crawl: queue selection completed. {len(url_list)} candidate videos")
        if url_list:
            preview_n = min(10, len(url_list))
            logger.info("Channel crawl: URL queue preview (first %d)", preview_n)
            for i in range(preview_n):
                logger.info("  [%d] %s", i + 1, url_list[i])
    else:
        input_df, output_df = load_dataframes(data_root)
        url_list = get_url_list(input_df, output_df)
        url_from_input_set = set(url_list) if url_list else set()
    
    if not url_list:
        logger.info("No videos to process. All videos have been processed.")
        return

    _run_batch_cleanup(config, dry_run_legacy=True)
    
    logger.info(f"Total videos to process: {len(url_list)}")
    
    # Process videos with progress bar
    counter = 0
    failed_urls = []
    consecutive_failures = 0
    last_success_time = time.time()
    
    min_wait = config['MIN_WAIT_BETWEEN_VIDEOS']
    max_wait = config['MAX_WAIT_BETWEEN_VIDEOS']
    extended_interval = config['EXTENDED_WAIT_INTERVAL']
    extended_duration = config['EXTENDED_WAIT_DURATION']
    max_consecutive_failures = config['MAX_CONSECUTIVE_FAILURES']
    failure_multiplier = config['FAILURE_WAIT_MULTIPLIER']
    queue_url_to_video_id = {}
    url_to_default_audio_lang = {}
    url_to_auto_subs_only = {}
    url_to_usage_channel = {}
    if config.get('CHANNEL_CRAWL') and meta_for_channel_crawl:
        queue_url_to_video_id = {
            str(m.get("url", "")): str(m.get("video_id", ""))
            for m in meta_for_channel_crawl
            if str(m.get("url", "")).strip()
        }
        url_to_default_audio_lang = {
            str(m.get("url", "")): str(m.get("default_audio_lang", "")).strip()
            for m in meta_for_channel_crawl
            if str(m.get("url", "")).strip() and str(m.get("default_audio_lang", "")).strip()
        }
        url_to_auto_subs_only = {
            str(m.get("url", "")): bool(m.get("auto_subs_only", False))
            for m in meta_for_channel_crawl
            if str(m.get("url", "")).strip() and m.get("auto_subs_only")
        }
        url_to_usage_channel = {
            str(m.get("url", "")): str(m.get("usage_channel", "")).strip()
            for m in meta_for_channel_crawl
            if str(m.get("url", "")).strip()
        }
    
    # When stdout is not a real TTY (launchd/cron redirect, or e.g. python main.py >> log),
    # tqdm may call flush() on that stream and raise OSError [Errno 11] on some macOS setups.
    # In scheduled/non-interactive runs, disable tqdm rendering entirely and never write to stdout.
    scheduled = bool(os.environ.get("LAUNCHD_SCHEDULED") or os.environ.get("CRON_SCHEDULED"))
    is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    disable_tqdm = scheduled or (not is_tty)
    tqdm_file = None
    if disable_tqdm:
        try:
            tqdm_file = open(os.devnull, "w")
        except OSError:
            tqdm_file = None
    tqdm_kw = {}
    if tqdm_file is not None:
        tqdm_kw["file"] = tqdm_file
    if disable_tqdm:
        tqdm_kw["disable"] = True

    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    set_pipeline_context(run_id=run_id, worker_id="w0")
    work_path_for_claim = config.get('WORK_PATH') or base_path
    claim_mgr = ClaimManager(work_path_for_claim)
    recovered = claim_mgr.recover_all_stale()
    if recovered:
        logger.info("Recovered %d stale claim(s): %s", len(recovered), recovered[:5])

    download_limiter = DownloadAdmissionLimiter(min_wait, max_wait)
    provider_cooldown = ProviderCooldown(base_wait=min_wait)

    def _append_catalog(wp: str, dr: str, entry: dict) -> None:
        from scripts.note_catalog_utils import append_catalog_entry
        append_catalog_entry(wp, dr, entry)

    state_writer = SharedStateWriter(
        data_root=data_root,
        output_df=output_df,
        output_df_path=output_df_path,
        save_output_df=_save_output_df_with_retry,
        crawl_queue_df=crawl_queue_df,
        save_crawl_queue=channel_crawl.save_crawl_queue_df,
        channel_crawl_enabled=bool(config.get('CHANNEL_CRAWL')),
        queue_url_to_video_id=queue_url_to_video_id,
        append_metadata_jsonl=stt.append_video_metadata_jsonl,
        append_catalog=_append_catalog,
        append_prompt_log=stt.prompt_log,
        prompt_log_path=prompt_log_path,
    )

    video_workers = int(config.get('VIDEO_WORKERS', 2))
    logger.info("Video workers: %d (device concurrency=%s)", video_workers, config.get('DEVICE_COMPUTE_CONCURRENCY', 1))

    worker_openai_clients: List[OpenAI] = [openai_client]
    worker_preprocessors: List[TranscriptPreprocessor] = [
        create_transcript_preprocessor(config.get("PREPROCESS_BACKEND", "cloud_api"), openai_client)
    ]
    if video_workers > 1:
        worker_openai_clients = [
            OpenAI(api_key=config['OPENAI_API_KEY']) for _ in range(video_workers)
        ]
        worker_preprocessors = [
            create_transcript_preprocessor(config.get("PREPROCESS_BACKEND", "cloud_api"), worker_openai_clients[i])
            for i in range(video_workers)
        ]

    def _video_config_for_url(v_url: str) -> dict:
        video_config = {**config}
        if url_to_default_audio_lang and v_url in url_to_default_audio_lang:
            video_config["default_audio_lang"] = url_to_default_audio_lang[v_url]
        if url_to_auto_subs_only and v_url in url_to_auto_subs_only:
            video_config["auto_subs_only"] = True
        if url_to_usage_channel and v_url in url_to_usage_channel:
            video_config["usage_channel"] = url_to_usage_channel[v_url]
        if v_url in url_from_input_set:
            video_config["from_input"] = True
        return video_config

    def _claim_vid_for_url(v_url: str) -> str:
        claim_vid = queue_url_to_video_id.get(v_url, "")
        if not claim_vid:
            try:
                claim_vid = stt.extract_youtube_id(v_url) or ""
            except (ValueError, TypeError):
                claim_vid = ""
        return claim_vid

    def _finalize_result(result: VideoProcessResult, v_url: str, claim_vid: str, i: int, pbar) -> None:
        nonlocal output_df, crawl_queue_df, consecutive_failures, counter, last_success_time
        state_writer.apply(result)
        output_df = state_writer.get_output_df()
        crawl_queue_df = state_writer.get_crawl_queue_df()
        if claim_vid:
            claim_mgr.release(claim_vid)
        elif result.video_id and result.video_id != "unknown":
            claim_mgr.release(result.video_id)
        status = result.status
        error_msg = result.error_message
        if status in ["error", "download_failed", "mlx_error", "api_error", "file_error"]:
            failed_urls.append((v_url, error_msg))
            consecutive_failures += 1
            download_limiter.record_failure(
                extended_duration=extended_duration * failure_multiplier,
                max_consecutive=max_consecutive_failures,
            )
        else:
            consecutive_failures = 0
            download_limiter.record_success()
            last_success_time = time.time()
        pbar.update(1)
        pbar.set_postfix({
            'Status': status,
            'Remaining': len(url_list) - i - 1,
            'Failures': consecutive_failures,
        })
        if video_workers <= 1:
            if status in ("success", "oversized_file"):
                sleep_time = random.uniform(min_wait, max_wait)
                if consecutive_failures > 0:
                    sleep_time *= failure_multiplier
                time.sleep(sleep_time)
                if counter % extended_interval == 0:
                    time.sleep(extended_duration)
            elif status in ("already_existed", "live_scheduled", "video_unavailable", "skipped_auto_subs_only"):
                time.sleep(random.uniform(min_wait, max_wait))
            elif failure_needs_long_cooldown(status, error_msg):
                time.sleep(random.uniform(min_wait * failure_multiplier, max_wait * failure_multiplier))
            else:
                time.sleep(random.uniform(min_wait, max_wait))

    def _worker_run(worker_idx: int, v_url: str, video_config: dict, claim_vid: str) -> VideoProcessResult:
        wid = f"w{worker_idx}"
        set_pipeline_context(run_id=run_id, worker_id=wid)
        try:
            return process_single_video(
                v_url=v_url,
                config=video_config,
                openai_client=worker_openai_clients[worker_idx],
                main_llm=main_llm,
                output_df=state_writer.get_output_df(),
                base_path=base_path,
                audio_path=audio_path,
                output_full_path=output_full_path,
                output_smm_path=output_smm_path,
                output_md_path=output_md_path,
                prompt_log_path=prompt_log_path,
                hf_path=hf_path,
                run_id=run_id,
                worker_id=wid,
                download_limiter=download_limiter,
                provider_cooldown=provider_cooldown,
                preprocessor=worker_preprocessors[worker_idx],
            )
        finally:
            clear_video_context()

    worker_rr = 0
    with tqdm(total=len(url_list), desc="Processing videos", unit="video", **tqdm_kw) as pbar:
        if video_workers <= 1:
            for i, v_url in enumerate(url_list):
                counter += 1
                pbar.set_description(f"Processing: {v_url[:50]}...")
                if consecutive_failures >= max_consecutive_failures:
                    download_limiter.set_extended_block(extended_duration * failure_multiplier)
                    consecutive_failures = 0
                video_config = _video_config_for_url(v_url)
                claim_vid = _claim_vid_for_url(v_url)
                if claim_vid and not claim_mgr.try_claim(
                    claim_vid, run_id, "w0",
                    source_queue="crawl" if config.get('CHANNEL_CRAWL') else "input",
                ):
                    logger.info("Skipping URL (active claim): %s vid=%s", v_url, claim_vid)
                    pbar.update(1)
                    continue
                result = _worker_run(0, v_url, video_config, claim_vid)
                _finalize_result(result, v_url, claim_vid, i, pbar)
        else:
            from concurrent.futures import wait, FIRST_COMPLETED
            with ThreadPoolExecutor(max_workers=video_workers) as executor:
                url_iter2 = iter(enumerate(url_list))
                in_flight = {}
                submitted = 0
                completed = 0
                while completed < len(url_list):
                    if consecutive_failures >= max_consecutive_failures:
                        download_limiter.set_extended_block(extended_duration * failure_multiplier)
                        consecutive_failures = 0
                    while submitted < len(url_list) and len(in_flight) < video_workers:
                        try:
                            i, v_url = next(url_iter2)
                        except StopIteration:
                            break
                        counter += 1
                        video_config = _video_config_for_url(v_url)
                        claim_vid = _claim_vid_for_url(v_url)
                        widx = worker_rr % video_workers
                        worker_rr += 1
                        if claim_vid and not claim_mgr.try_claim(
                            claim_vid, run_id, f"w{widx}",
                            source_queue="crawl" if config.get('CHANNEL_CRAWL') else "input",
                        ):
                            logger.info("Skipping URL (active claim): %s vid=%s", v_url, claim_vid)
                            pbar.update(1)
                            completed += 1
                            continue
                        fut = executor.submit(_worker_run, widx, v_url, video_config, claim_vid)
                        in_flight[fut] = (i, v_url, claim_vid)
                        submitted += 1
                    if not in_flight:
                        break
                    done, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
                    for fut in done:
                        i, v_url, claim_vid = in_flight.pop(fut)
                        result = fut.result()
                        pbar.set_description(f"Processing: {v_url[:50]}...")
                        _finalize_result(result, v_url, claim_vid, i, pbar)
                        completed += 1
    if tqdm_file is not None:
        tqdm_file.close()
    
    # Summary with detailed statistics
    logger.info("=" * 60)
    logger.info("Processing completed!")
    logger.info("=" * 60)
    
    # Calculate statistics
    recent_df = output_df.tail(len(url_list)) if len(output_df) >= len(url_list) else output_df
    status_counts = recent_df['status'].value_counts().to_dict()
    
    total_processed = len(url_list)
    successful = status_counts.get('success', 0)
    already_existed = status_counts.get('already_existed', 0)
    oversized_file = status_counts.get('oversized_file', 0)
    passed_shorts = status_counts.get(channel_crawl.SHORTS_STATUS, 0)
    live_scheduled = status_counts.get('live_scheduled', 0)
    video_unavailable = status_counts.get('video_unavailable', 0)
    skipped_auto_subs_only = status_counts.get(channel_crawl.SKIPPED_AUTO_SUBS_ONLY_STATUS, 0)
    download_failed = status_counts.get('download_failed', 0)
    mlx_error = status_counts.get('mlx_error', 0)
    api_error = status_counts.get('api_error', 0)
    file_error = status_counts.get('file_error', 0)
    other_errors = total_processed - successful - already_existed - oversized_file - passed_shorts - live_scheduled - video_unavailable - skipped_auto_subs_only - download_failed - mlx_error - api_error - file_error
    
    logger.info(f"📊 Processing Statistics:")
    logger.info(f"  Total videos processed: {total_processed}")
    logger.info(f"  ✓ Successful: {successful} ({successful/total_processed*100:.1f}%)")
    logger.info(f"  ⊘ Already existed: {already_existed}")
    logger.info(f"  ⊘ Skipped (oversized file): {oversized_file}")
    logger.info(f"  ⊘ Skipped (shorts): {passed_shorts}")
    logger.info(f"  ⊘ Skipped (live/scheduled live): {live_scheduled}")
    logger.info(f"  ⊘ Skipped (video unavailable/private): {video_unavailable}")
    logger.info(f"  ⊘ Skipped (auto_subs_only, no subs): {skipped_auto_subs_only}")
    logger.info(f"  ✗ Download failed: {download_failed}")
    logger.info(f"  ✗ Transcription error: {mlx_error}")
    logger.info(f"  ✗ API error: {api_error}")
    logger.info(f"  ✗ File error: {file_error}")
    if other_errors > 0:
        logger.info(f"  ✗ Other errors: {other_errors}")
    if shorts_recorded_count > 0:
        logger.info(f"  Queue discovery-time shorts recorded: {shorts_recorded_count}")
    
    logger.info(f"")
    logger.info(f"Success rate: {successful/(total_processed-already_existed)*100:.1f}%" if (total_processed-already_existed) > 0 else "N/A")

    if config.get('CHANNEL_CRAWL'):
        try:
            channel_crawl.update_channel_last_processed_from_queue(data_root, crawl_queue_df)
            logger.info("Updated channel_df.csv last_processed_published_at from crawl queue(done).")
        except Exception as e:
            logger.warning(f"Failed to update channel_df: {e}")

    if successful > 0:
        try:
            from scripts.build_daily_digest import build_digest

            today = datetime.now().strftime("%Y-%m-%d")
            wp = (config.get("WORK_PATH") or "").strip()
            build_digest(
                config["OUTPUT_MD_PATH"],
                wp,
                data_root,
                today,
            )
            logger.info("Daily digest updated: digest/%s.md", today.replace("-", "_"))
        except Exception as de:
            logger.warning("Daily digest build failed (non-fatal): %s", de)

        try:
            from scripts.drive_yt_summary.sync import run_sync_safe

            drive_result = run_sync_safe()
            if drive_result.errors:
                logger.warning(
                    "Drive YT_summary sync failed (non-fatal): %s",
                    "; ".join(drive_result.error_messages) or drive_result.as_dict(),
                )
            else:
                logger.info(
                    "Drive YT_summary sync OK: created=%s updated=%s skipped=%s",
                    drive_result.created,
                    drive_result.updated,
                    drive_result.skipped,
                )
        except Exception as ds:
            logger.warning("Drive YT_summary sync failed (non-fatal): %s", ds)

    _run_batch_cleanup(config, dry_run_legacy=False)
    
    if failed_urls:
        logger.warning("")
        logger.warning("=" * 60)
        logger.warning("Failed URLs Summary:")
        logger.warning("=" * 60)
        
        # Group errors by type
        error_groups = {}
        for url, error in failed_urls:
            error_type = error.split(':')[0] if ':' in error else "Unknown"
            if error_type not in error_groups:
                error_groups[error_type] = []
            error_groups[error_type].append((url, error))
        
        for error_type, errors in error_groups.items():
            logger.warning(f"\n{error_type} ({len(errors)} occurrences):")
            for url, error in errors[:5]:  # Show first 5 of each type
                logger.warning(f"  - {url}")
                logger.warning(f"    Error: {error}")
            if len(errors) > 5:
                logger.warning(f"  ... and {len(errors) - 5} more")
        
        # Save failed URLs to file
        failed_urls_file = os.path.join(data_root, 'failed_urls.txt')
        try:
            with open(failed_urls_file, 'w', encoding='utf-8') as f:
                f.write(f"Failed URLs - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n\n")
                for url, error in failed_urls:
                    f.write(f"URL: {url}\n")
                    f.write(f"Error: {error}\n")
                    f.write("-" * 60 + "\n")
            logger.info(f"\nFailed URLs saved to: {failed_urls_file}")
        except Exception as e:
            logger.warning(f"Failed to save failed URLs file: {str(e)}")

def initialize_mlx_model(config: dict, logger: logging.Logger) -> bool:
    """
    Initialize MLX Whisper model once at startup.
    This pre-loads the model to avoid reloading for each video.
    
    Args:
        config: Configuration dictionary
        logger: Logger instance
    
    Returns:
        True if model initialized successfully, False otherwise
    """
    try:
        hf_path = config.get('HF_HOME')
        if not hf_path:
            logger.warning("HF_HOME not set, skipping model pre-load")
            return False
        
        logger.info("=" * 60)
        logger.info("Initializing MLX Whisper model (한 번만 로드하여 재사용)")
        logger.info(f"  Model path: {hf_path}")
        logger.info("  This may take a moment on first run...")
        logger.info("=" * 60)
        
        # Pre-load the model (default: turbo)
        model = stt.load_mlx_model(hf_path, mlx_model="turbo")
        
        if model is not None:
            logger.info("✓ MLX Whisper model initialized successfully")
            logger.info("  Model will be reused for all transcriptions (빠른 처리)")
            logger.info("=" * 60)
            return True
        else:
            logger.warning("Failed to pre-load model, will load on first transcription")
            return False
            
    except Exception as e:
        error_type = type(e).__name__
        logger.warning(f"[WARNING] Failed to pre-load MLX Whisper model")
        logger.warning(f"  Exception Type: {error_type}")
        logger.warning(f"  Error: {str(e)}")
        logger.warning("  Will load model on first transcription (slower)")
        return False

def main():
    """Main entry point."""
    start_time = time.time()
    lock_handle = None
    
    try:
        logger.info("=" * 60)
        logger.info(f"Speech-to-Text v{APP_VERSION} - Starting Application")
        logger.info(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        
        config = load_config()
        logger.info("Configuration loaded successfully")

        run_source = "scheduled" if (os.environ.get("LAUNCHD_SCHEDULED") or os.environ.get("CRON_SCHEDULED")) else "manual"
        acquired, lock_handle, lock_message = run_lock.acquire_run_lock(
            config.get("BASE_PATH", "."),
            run_source,
            bool(config.get("CHANNEL_CRAWL")),
        )
        if not acquired:
            logger.error(lock_message)
            return
        logger.info("Run lock acquired (source=%s, channel_crawl=%s)", run_source, bool(config.get("CHANNEL_CRAWL")))
        
        scheduled_env = bool(os.environ.get("LAUNCHD_SCHEDULED") or os.environ.get("CRON_SCHEDULED"))

        # 스케줄로 실행된 경우: CHANNEL_CRAWL=false면 실행하지 않고 종료 (input_df 모드는 수동 실행만)
        if scheduled_env and not config.get("CHANNEL_CRAWL"):
            logger.info("CHANNEL_CRAWL=false and launched by schedule; skipping run. Use manual run for input_df mode.")
            return
        
        # 수동 실행인데 launchd가 로드돼 있으면 경고 후 종료 (이중 프로세스 방지; 수동 실행은 launchd unload 후에만)
        if not scheduled_env:
            try:
                r = subprocess.run(
                    ["launchctl", "list"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=config.get("BASE_PATH", "."),
                )
                if r.returncode == 0 and "com.user.p03-speech2text" in (r.stdout or ""):
                    logger.warning(
                        "launchd job 'com.user.p03-speech2text' is loaded. Exiting to avoid two processes. "
                        "To run main.py manually (input_df or channel crawl), unload launchd first (see docs/SCHEDULING.md)."
                    )
                    sys.exit(0)
            except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
                pass
        
        # Initialize MLX Whisper model once at startup (optimization)
        initialize_mlx_model(config, logger)
        
        process_videos(config)
        
        elapsed_time = time.time() - start_time
        logger.info("=" * 60)
        logger.info("Application completed successfully")
        logger.info(f"Total execution time: {elapsed_time/3600:.2f} hours ({elapsed_time/60:.1f} minutes)")
        logger.info(f"End time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        
    except KeyboardInterrupt:
        elapsed_time = time.time() - start_time
        logger.warning("")
        logger.warning("=" * 60)
        logger.warning("Process interrupted by user (Ctrl+C)")
        logger.warning(f"Execution time before interrupt: {elapsed_time/3600:.2f} hours ({elapsed_time/60:.1f} minutes)")
        logger.warning("=" * 60)
        logger.warning("Progress has been saved. You can resume by running the script again.")
        sys.exit(1)
        
    except FileNotFoundError as e:
        logger.error("")
        logger.error("=" * 60)
        logger.error("[FATAL ERROR] FileNotFoundError")
        logger.error(f"  Error: {str(e)}")
        logger.error("  Solution: Check file paths in .env and ensure all required files exist")
        logger.error("=" * 60)
        sys.exit(1)
        
    except ValueError as e:
        logger.error("")
        logger.error("=" * 60)
        logger.error("[FATAL ERROR] ValueError - Configuration Error")
        logger.error(f"  Error: {str(e)}")
        logger.error("  Solution: Check .env file and ensure all required settings are correct")
        logger.error("=" * 60)
        sys.exit(1)
        
    except Exception as e:
        elapsed_time = time.time() - start_time
        error_type = type(e).__name__
        logger.error("")
        logger.error("=" * 60)
        logger.error(f"[FATAL ERROR] {error_type}")
        logger.error(f"  Error: {str(e)}")
        logger.error(f"  Execution time before error: {elapsed_time/3600:.2f} hours ({elapsed_time/60:.1f} minutes)")
        logger.error("  Stack trace:")
        logger.error("=" * 60)
        logger.error("", exc_info=True)
        logger.error("=" * 60)
        logger.error("Please check the logs for detailed error information.")
        sys.exit(1)
    finally:
        if lock_handle is not None:
            try:
                lock_handle.release()
                logger.info("Run lock released.")
            except Exception:
                pass

if __name__ == "__main__":
    main()
