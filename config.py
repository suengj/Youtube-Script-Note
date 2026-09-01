# -*- coding: utf-8 -*-
"""
앱 설정 (비밀키·경로 제외). .env는 API 키·경로만, 여기는 Rate Limiting·오디오 임계값·압축·채널 크롤 등.
main.py, zip_process.py에서 import하여 사용.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

# Application semver (v4.2: 2-worker concurrency, job workspace, single writer)
APP_VERSION = "4.2.0"
NOTE_CATALOG_SCHEMA_VERSION = 1

# MB 단위. output.wav(변환 후) 용량이 이 값 이상이면 Whisper 전사 생략 (OOM 방지).
# 16GB 맥북: 600~800 권장.
AUDIO_SIZE_THRESHOLD_MB = 768

# 비디오 간 대기 시간 (초)
MIN_WAIT_BETWEEN_VIDEOS = 30
MAX_WAIT_BETWEEN_VIDEOS = 40
# N개 비디오마다 EXTENDED_WAIT_DURATION 초만큼 확장 대기
EXTENDED_WAIT_INTERVAL = 20
EXTENDED_WAIT_DURATION = 300
# 연속 실패 허용 횟수 (초과 시 긴 대기)
MAX_CONSECUTIVE_FAILURES = 10
# 실패 시 대기 시간 배수 (main.py: 네트워크·레이트리밋·일시적 I/O 등에만 적용; 비공개/자막 실패 등은 배수 없음)
FAILURE_WAIT_MULTIPLIER = 6


# ---- 채널 크롤: False = input_df만 사용, True = channel_df.csv 기반 수집
# CHANNEL_BACKFILL False = last 이후 증분만(last 필수), True = 과거 구간 수집(END_DATE 필수, START_DATE 선택)
CHANNEL_CRAWL = True
CHANNEL_BACKFILL = False
CHANNEL_START_DATE = ""
CHANNEL_END_DATE = ""

# ---- 배치 cycle: 채널 조회 후 다운로드·Whisper 실행 방식
# CHANNEL_BATCH_MODE: update_then_process(1단계→2단계 한 번에), update_only(채널 조회만), process_only(output_df만 비교)
## 현재 update_only, process_only는 구축되어 있지 않음
CHANNEL_BATCH_MODE = "update_then_process"

# CHANNEL_BATCH_INTERVAL_HOURS: 주기(시간). 0=매 실행 시, 1=1시간마다, 24=하루마다. 실제 스케줄은 cron/launchd가 참고
CHANNEL_BATCH_INTERVAL_HOURS = 12

# ---- 채널 크롤 추가 옵션
# FILTERING_SHORTS_MINUTES: 이 값(분) 이하 영상은 Shorts로 간주하여 channel_crawl 후보에서 제외
FILTERING_SHORTS_MINUTES = 3
# CRAWL_QUEUE_MAX_RETRIES: crawl_yt_list.csv에서 failed 자동 재시도 최대 횟수
CRAWL_QUEUE_MAX_RETRIES = 2

# ---- Nano preprocess (Phase 1c) ----
# Retention = fraction of original length to keep after minimization (min%, max%).
NANO_RETENTION_DEFAULT = (80, 95)  # whisper / uploader subs (noisy speech)
NANO_RETENTION_AUTO_SUBS = (60, 80)  # auto_subs already cleaner; tighter OK
# Chunked minimization: skip final merge pass when combined output still over limit
SKIP_MERGE_REMINIMIZE = True

# YT_DOWNLOAD_IF_SUBS_Y: True = 업로더 자막이 있어도 영상 다운로드 실행. False = 업로더 자막 있으면 영상 다운로드 생략(자막만 다운로드).
# 자막 존재 시 Whisper는 항상 생략하고 자막으로 summary 진행.
YT_DOWNLOAD_IF_SUBS_Y = False
# YOUTUBE_AUTO_SCRIPT: True = 업로더 없을 때(또는 업로더 다운로드 실패 시) YouTube 자동 자막 시도. False면 오디오+Whisper만.
YOUTUBE_AUTO_SCRIPT = True
# YOUTUBE_SUBS_LANGS: 자막/자동자막 다운로드 시 사용할 언어 코드 (쉼표 구분). 예: en,ko,jp,en-US,en-GB
YOUTUBE_SUBS_LANGS = "en,ko,jp,ja,en-US,en-GB"
# SAVE_FULL_WHEN_AUTO_SUBS: True = auto_subs 사용 시에도 output_new/full/ 저장. False = auto_subs 시 full 저장 생략 (향후 개선 예정).
SAVE_FULL_WHEN_AUTO_SUBS = False
# FILENAME_MAX_LENGTH: sanitize_filename에서 제목(base) 최대 길이. 0이면 제한 없음.
FILENAME_MAX_LENGTH = 50

# ---- Job workspace & transcript cache (v4.2) ----
USE_JOB_WORKSPACE = True
TRANSCRIPT_CACHE_ENABLED = True
TRANSCRIPT_CACHE_TTL_HOURS = 72
SUBTITLE_QUARANTINE_DAYS = 7
STALE_JOB_MAX_AGE_HOURS = 6

# ---- Worker concurrency (v4.2) ----
VIDEO_WORKERS = 2  # clamped to max 2 at runtime
DEVICE_COMPUTE_CONCURRENCY = 1
PREPROCESS_BACKEND = "cloud_api"  # cloud_api | on_device (stub)

### CRON/LAUNCHD 설정 방법 간략하게 설명
# launchd (권장):
#   ./scripts/install_launchd.sh
#   plist: WORK_PATH=프로젝트 루트, TMPDIR/XDG_CACHE_HOME=…/tmp, …/cache
#   4. 강제 실행 (테스트):
#      launchctl kickstart -k gui/$(id -u)/com.user.p03-speech2text
#   5. 상태 확인:
#      launchctl list | grep p03-speech2text
#   6. 중지:
#      launchctl stop gui/$(id -u)/com.user.p03-speech2text
#   7. 해제 (스케줄 제거):
#      launchctl bootout gui/$(id -u)/com.user.p03-speech2text
# cron:
#   crontab -e  # 편집
#   0 */3 * * * cd ~/Developer/PJT/p03_speech2text && /opt/homebrew/Caskroom/miniforge/base/envs/ai/bin/python main.py >> logs/cron.log 2>&1
# 상세: docs/SCHEDULING.md 참고


# ---- M4A archive compression (zip_process.py). Set COMPRESSION_AUDIO_PATH in .env.
COMPRESSION_AUDIO_PATH = (os.getenv("COMPRESSION_AUDIO_PATH") or "").strip()
COMPRESSION_MODE = "unzip"
COMPRESSION_METHOD = "zstd"
ZSTD_LEVEL = 19
COMPRESSION_STATE_FILE = "compression_state.json"
COMPRESSION_RECURSIVE = False
# MB 단위. 이 값 미만 M4A는 압축 생략 (작은 파일은 zstd 후 오히려 커질 수 있음). 0이면 제한 없음.
COMPRESSION_MIN_SIZE_MB = 100
# unzip 후 복원 성공 시 .m4a.zst(또는 .7z) 및 .meta.json 삭제 여부
COMPRESSION_DELETE_AFTER_UNZIP = True


def resolve_data_root(
    base_path: str,
    work_path: Optional[str] = None,
) -> str:
    """
    Directory for hot, frequently updated files (CSV queue, output_df, append jsonl).
    - If env DATA_ROOT is set (after load_dotenv): use it.
    - Else if WORK_PATH is set: ``{WORK_PATH}/data`` (recommended with local WORK_PATH).
    - Else: BASE_PATH (legacy: files next to main.py on iCloud).
    """
    override = os.getenv("DATA_ROOT", "").strip()
    if override:
        return os.path.abspath(override)
    if work_path:
        wp = os.path.abspath(work_path)
        bp = os.path.abspath(base_path)
        # Only use WORK_PATH/data when WORK_PATH is a distinct volume/dir (local scratch)
        if wp != bp:
            return os.path.join(wp, "data")
    return os.path.abspath(base_path)


def apply_work_path_scratch_env() -> None:
    """
    If WORK_PATH is set and TMPDIR / XDG_CACHE_HOME are not, point them under WORK_PATH
    (tmp/, cache/) so yt-dlp, tempfile, and libraries avoid iCloud or odd defaults.
    Call after load_dotenv(). Does not override explicit env.
    """
    work = os.getenv("WORK_PATH", "").strip()
    if not work:
        return
    try:
        tmp = os.path.join(work, "tmp")
        cache = os.path.join(work, "cache")
        os.makedirs(tmp, exist_ok=True)
        os.makedirs(cache, exist_ok=True)
    except OSError:
        return
    if not os.getenv("TMPDIR", "").strip():
        os.environ["TMPDIR"] = tmp
    if not os.getenv("XDG_CACHE_HOME", "").strip():
        os.environ["XDG_CACHE_HOME"] = cache


def path_likely_errno11_risk_macos(path: str) -> bool:
    """
    Heuristic: paths under iCloud Drive, Mobile Documents, or CloudStorage (OneDrive/Dropbox/etc.)
    are more likely to hit OSError errno 11 (often EDEADLK / 'Resource deadlock avoided') on macOS
    when background jobs rename or stream-write files.
    """
    if not path or not str(path).strip():
        return False
    try:
        norm = os.path.realpath(os.path.expanduser(str(path).strip()))
    except OSError:
        norm = os.path.expanduser(str(path).strip())
    low = norm.replace("\\", "/").lower()
    markers = (
        "/library/mobile documents/",
        "com~apple~clouddocs",
        "icloud~",
        "/library/cloudstorage/",
    )
    return any(m in low for m in markers)


def log_macos_deadlock_path_warnings(
    logger: logging.Logger,
    *,
    data_root: Optional[str] = None,
    work_path: Optional[str] = None,
    base_path: Optional[str] = None,
    tmpdir: Optional[str] = None,
    xdg_cache: Optional[str] = None,
) -> None:
    """
    Log one warning per risky path so operators can fix WORK_PATH/DATA_ROOT/TMPDIR before yt-dlp fails.
    Safe to call when any path is None.
    """
    checks = [
        ("DATA_ROOT", data_root),
        ("WORK_PATH", work_path),
        ("BASE_PATH", base_path),
        ("TMPDIR", tmpdir),
        ("XDG_CACHE_HOME", xdg_cache),
    ]
    for label, p in checks:
        if not p:
            continue
        if path_likely_errno11_risk_macos(p):
            logger.warning(
                "macOS Errno11 risk: %s may be on iCloud/sync storage (%s). "
                "Prefer local disk (e.g. ~/YTT_AUDIO) for WORK_PATH, DATA_ROOT, TMPDIR, XDG_CACHE_HOME.",
                label,
                p,
            )


def get_config_dict():
    """main.py용: Rate limiting·오디오 임계값·채널 크롤만 담은 dict (config_defaults 키만)."""
    return {
        "AUDIO_SIZE_THRESHOLD_MB": AUDIO_SIZE_THRESHOLD_MB,
        "MIN_WAIT_BETWEEN_VIDEOS": MIN_WAIT_BETWEEN_VIDEOS,
        "MAX_WAIT_BETWEEN_VIDEOS": MAX_WAIT_BETWEEN_VIDEOS,
        "EXTENDED_WAIT_INTERVAL": EXTENDED_WAIT_INTERVAL,
        "EXTENDED_WAIT_DURATION": EXTENDED_WAIT_DURATION,
        "MAX_CONSECUTIVE_FAILURES": MAX_CONSECUTIVE_FAILURES,
        "FAILURE_WAIT_MULTIPLIER": FAILURE_WAIT_MULTIPLIER,
        "CHANNEL_CRAWL": CHANNEL_CRAWL,
        "CHANNEL_BACKFILL": CHANNEL_BACKFILL,
        "CHANNEL_START_DATE": CHANNEL_START_DATE,
        "CHANNEL_END_DATE": CHANNEL_END_DATE,
        "CHANNEL_BATCH_MODE": CHANNEL_BATCH_MODE,
        "CHANNEL_BATCH_INTERVAL_HOURS": CHANNEL_BATCH_INTERVAL_HOURS,
        "FILTERING_SHORTS_MINUTES": FILTERING_SHORTS_MINUTES,
        "CRAWL_QUEUE_MAX_RETRIES": CRAWL_QUEUE_MAX_RETRIES,
        "YT_DOWNLOAD_IF_SUBS_Y": YT_DOWNLOAD_IF_SUBS_Y,
        "YOUTUBE_AUTO_SCRIPT": YOUTUBE_AUTO_SCRIPT,
        "YOUTUBE_SUBS_LANGS": YOUTUBE_SUBS_LANGS,
        "SAVE_FULL_WHEN_AUTO_SUBS": SAVE_FULL_WHEN_AUTO_SUBS,
        "FILENAME_MAX_LENGTH": FILENAME_MAX_LENGTH,
        "APP_VERSION": APP_VERSION,
        "NANO_RETENTION_DEFAULT": NANO_RETENTION_DEFAULT,
        "NANO_RETENTION_AUTO_SUBS": NANO_RETENTION_AUTO_SUBS,
        "SKIP_MERGE_REMINIMIZE": SKIP_MERGE_REMINIMIZE,
        "USE_JOB_WORKSPACE": USE_JOB_WORKSPACE,
        "TRANSCRIPT_CACHE_ENABLED": TRANSCRIPT_CACHE_ENABLED,
        "TRANSCRIPT_CACHE_TTL_HOURS": TRANSCRIPT_CACHE_TTL_HOURS,
        "SUBTITLE_QUARANTINE_DAYS": SUBTITLE_QUARANTINE_DAYS,
        "STALE_JOB_MAX_AGE_HOURS": STALE_JOB_MAX_AGE_HOURS,
        "VIDEO_WORKERS": VIDEO_WORKERS,
        "DEVICE_COMPUTE_CONCURRENCY": DEVICE_COMPUTE_CONCURRENCY,
        "PREPROCESS_BACKEND": PREPROCESS_BACKEND,
    }


def get_compression_config_dict():
    """zip_process.py용: COMPRESSION_* 만 담은 dict."""
    return {
        "COMPRESSION_AUDIO_PATH": COMPRESSION_AUDIO_PATH,
        "COMPRESSION_MODE": COMPRESSION_MODE,
        "COMPRESSION_METHOD": COMPRESSION_METHOD,
        "ZSTD_LEVEL": ZSTD_LEVEL,
        "COMPRESSION_STATE_FILE": COMPRESSION_STATE_FILE,
        "COMPRESSION_RECURSIVE": COMPRESSION_RECURSIVE,
        "COMPRESSION_MIN_SIZE_MB": COMPRESSION_MIN_SIZE_MB,
        "COMPRESSION_DELETE_AFTER_UNZIP": COMPRESSION_DELETE_AFTER_UNZIP,
    }
