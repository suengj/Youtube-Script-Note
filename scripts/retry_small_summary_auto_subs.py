#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
summary 폴더 내 1KB 미만 파일 재처리: auto subs 다운로드 → chunk 기반 요약 → summary/MD 저장.

1KB 미만 파일은 input text가 너무 길어 token minimization/summary 단계에서 실패한 것으로 추정.
VID 추출 후 auto subs만 다시 받아 chunk 처리로 재작업.

- summary: 기존 small 파일을 덮어쓰기 (대치)
- MD: OUTPUT_MD_PATH 내 기존 MD 경로가 있으면 그 위에 덮어쓰기, 없으면 신규 생성

Usage:
  python scripts/retry_small_summary_auto_subs.py [--base-path DIR] [--size-limit KB] [--dry-run]
  --base-path   프로젝트 경로 (default: BASE_PATH from .env)
  --size-limit  이 값(KB) 미만 파일만 대상 (default: 1)
  --dry-run     대상 VID만 출력, 실제 처리 안 함
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

# VID: 11 chars before _ko-orig, _en, etc. or at start for VID-only
VID_IN_SUFFIX = re.compile(r"_([A-Za-z0-9_-]{11})_(?:ko-orig|en-orig|ko|en|ja|en-US|en-GB)_(?:auto_subs|subs)_5-mini\.txt$")
VID_ONLY_START = re.compile(r"^([A-Za-z0-9_-]{11})_")

SIZE_LIMIT_BYTES = 1024  # 1KB
from pathlib import Path

DEFAULT_BASE = str(Path(__file__).resolve().parents[1])

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def extract_vid_from_summary_filename(name: str) -> str | None:
    """Extract 11-char VID from summary filename. Returns None if not found."""
    if not name.endswith(".txt"):
        return None
    # VID-only: xHi8PUIVyoo_auto_subs_5-mini.txt
    m = VID_ONLY_START.match(name[:-4])
    if m:
        return m.group(1)
    # Title_VID_ko-orig_auto_subs_5-mini.txt
    m = VID_IN_SUFFIX.search(name)
    if m:
        return m.group(1)
    return None


def find_small_summary_files(summary_dir: str, size_limit_bytes: int) -> list[tuple[str, str]]:
    """Return list of (filepath, vid) for .txt files under size_limit_bytes."""
    if not os.path.isdir(summary_dir):
        return []
    results = []
    for name in os.listdir(summary_dir):
        if not name.endswith(".txt"):
            continue
        path = os.path.join(summary_dir, name)
        if not os.path.isfile(path):
            continue
        if os.path.getsize(path) >= size_limit_bytes:
            continue
        vid = extract_vid_from_summary_filename(name)
        if vid:
            results.append((path, vid))
        else:
            logger.debug("Skip (no VID): %s", name)
    return results


def find_existing_md_for_vid(output_md_path: str, vid: str) -> str | None:
    """Find existing MD file for VID under OUTPUT_MD_PATH (YYYY_MM_DD subdirs). Returns path or None."""
    if not output_md_path or not os.path.isdir(output_md_path):
        return None
    for name in os.listdir(output_md_path):
        subdir = os.path.join(output_md_path, name)
        if not os.path.isdir(subdir):
            continue
        if not re.match(r"^\d{4}_\d{2}_\d{2}$", name):
            continue
        for fname in os.listdir(subdir):
            if not fname.lower().endswith(".md"):
                continue
            if vid in fname:
                return os.path.join(subdir, fname)
    return None


def process_one_vid(
    vid: str,
    base_path: str,
    config: dict,
    openai_client,
    main_llm,
    dry_run: bool,
    summary_target_path: str | None = None,
    md_target_path: str | None = None,
) -> bool:
    """Download auto subs, chunk process, save summary + MD. Returns True on success."""
    import stt_function_v3 as stt

    if not stt.YT_DLP_AVAILABLE:
        logger.error("yt-dlp not available")
        return False

    URL = f"https://www.youtube.com/watch?v={vid}"
    work_path = config.get("WORK_PATH") or base_path
    subs_dir = os.path.join(work_path, "yt_subs")
    output_smm_path = os.path.join(base_path, "output_new", "summary")
    output_md_path = config.get("OUTPUT_MD_PATH", "")
    if not output_md_path:
        output_md_path = os.getenv("OUTPUT_MD_PATH", "")

    # Prompts (same as main.py)
    from main import INPUT_PROMPT, MAIN_LLM_TOKEN_RANGE, TOKEN_INPUT_ROLE, build_token_query, initialize_clients, load_config  # noqa: E402

    if dry_run:
        logger.info("[DRY-RUN] Would process VID: %s", vid)
        return True

    # 1. extract_info for title, channel, upload_date
    try:
        ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "extract_flat": False}
        with stt.yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(URL, download=False)
    except Exception as e:
        logger.error("extract_info failed for %s: %s", vid, e)
        return False

    if not info:
        logger.error("extract_info returned None for %s", vid)
        return False

    title = info.get("title") or f"video_{vid}"
    channel_name = info.get("uploader") or info.get("channel") or ""
    upload_date = stt._format_upload_date(info)

    # 2. Download auto subs
    subs_langs = stt._parse_subs_langs(config.get("YOUTUBE_SUBS_LANGS"))
    vd_hint = float(info["duration"]) if info.get("duration") else None
    result = stt._yt_download_auto_subs_only(
        URL,
        vid,
        subs_dir,
        logger,
        subs_langs=subs_langs,
        prefer_lang=config.get("default_audio_lang"),
        info=info,
        video_duration_sec=vd_hint,
    )
    if not result:
        logger.error("Auto subs download failed for %s", vid)
        return False
    subs_path, subs_lang = result

    # 3. Transcription
    transcription = stt.subtitle_file_to_plain_text(subs_path)
    if not transcription or len(transcription.strip()) < 50:
        logger.error("Empty or too short transcription for %s", vid)
        return False

    a_min, a_max = int((config.get("NANO_RETENTION_AUTO_SUBS") or (60, 80))[0]), int((config.get("NANO_RETENTION_AUTO_SUBS") or (60, 80))[1])
    token_query = build_token_query(a_min, a_max, auto_subs=True)
    skip_merge = bool(config.get("SKIP_MERGE_REMINIMIZE", True))
    concise_transcription = stt.token_minimizer_chunked(
        TOKEN_INPUT_ROLE, token_query, transcription, openai_client,
        model=config.get('PREPROCESS_LLM_MODEL', 'gpt-5-nano-2025-08-07'),
        skip_merge_reminimize=skip_merge,
    )

    # 5. Save concise to summary (overwrite existing small file)
    if summary_target_path:
        concise_path = summary_target_path
    else:
        max_len = config.get("FILENAME_MAX_LENGTH", 50) or 0
        base_name = stt.sanitize_filename(title, max_length=max_len if max_len > 0 else 99999)
        if not base_name:
            base_name = vid
        elif vid not in base_name:
            base_name = f"{base_name}_{vid}"
        lang_suffix = f"_{subs_lang}" if subs_lang else ""
        txt_file_name = f"{base_name}{lang_suffix}_auto_subs.txt"
        output_file = stt.change_filename(txt_file_name, f"_{config.get('MAIN_LLM_OUTPUT_SUFFIX', '5-mini')}")
        concise_path = os.path.join(output_smm_path, output_file)
    os.makedirs(os.path.dirname(concise_path), exist_ok=True)
    with open(concise_path, "w", encoding="utf-8-sig") as f:
        f.write(concise_transcription)
    logger.info("Saved summary (overwrite): %s", concise_path)

    # 6. Summarize (chunked)
    audio_nm = (info.get("title") or f"video_{vid}")[:80]
    response = main_llm.summarize(
        transcription=concise_transcription,
        filename=audio_nm,
        prompt=INPUT_PROMPT,
        token_range=list(MAIN_LLM_TOKEN_RANGE),
        language="Korean",
        style="Markdown",
    )

    # 7. Save MD (overwrite existing or create new)
    from datetime import datetime
    v_date = datetime.now().strftime("%Y-%m-%d")
    existing_md = find_existing_md_for_vid(output_md_path, vid) if output_md_path else None
    if not output_md_path:
        md_path = None
    elif existing_md or md_target_path:
        md_path = md_target_path or existing_md
    else:
        max_len = config.get("FILENAME_MAX_LENGTH", 50) or 0
        base_name = stt.sanitize_filename(title, max_length=max_len if max_len > 0 else 99999)
        if not base_name:
            base_name = vid
        elif vid not in base_name:
            base_name = f"{base_name}_{vid}"
        lang_suffix = f"_{subs_lang}" if subs_lang else ""
        txt_file_name = f"{base_name}{lang_suffix}_auto_subs.txt"
        output_file = stt.change_filename(txt_file_name, f"_{config.get('MAIN_LLM_OUTPUT_SUFFIX', '5-mini')}")
        channel_prefix = re.sub(r'[/\\:*?"<>|]', '_', channel_name.strip()) if channel_name else "unknown"
        md_filename = stt.change_extension(output_file, "md")
        if channel_prefix:
            md_filename = f"{channel_prefix}_{md_filename}"
        date_folder = v_date.replace("-", "_")
        date_dir = os.path.join(output_md_path, date_folder)
        os.makedirs(date_dir, exist_ok=True)
        md_path = os.path.join(date_dir, md_filename)

    if md_path:
        from scripts.md_mobile_utils import assemble_mobile_md, build_save_entry, prepare_mobile_body
        from scripts.note_catalog_utils import append_catalog_entry

        body, tags, md_title, tldr = prepare_mobile_body(response)
        if not md_title:
            md_title = (title or "")[:120]
        ch_prefix = re.sub(r'[/\\:*?"<>|]', '_', channel_name.strip()) if channel_name else "unknown"
        suffix = config.get("MAIN_LLM_OUTPUT_SUFFIX", "5-mini")
        ud = upload_date[:10] if upload_date and len(upload_date) >= 10 else ""
        save_entry = build_save_entry(
            md_abs_path=md_path,
            md_root=output_md_path,
            vid=vid,
            channel=ch_prefix,
            upload_date=ud,
            transcript_date=v_date,
            lang=(subs_lang or "ko").lower(),
            suffix=suffix,
            source_url=URL,
            tags=tags,
            title=md_title,
            tldr=tldr,
        )
        content = assemble_mobile_md(save_entry, body)
        with open(md_path, "w", encoding="utf-8-sig") as f:
            f.write(content)
        logger.info("Saved MD (%s): %s", "overwrite" if (existing_md or md_target_path) else "new", md_path)
        try:
            dr = config.get("DATA_ROOT")
            if not dr:
                from config import resolve_data_root
                dr = resolve_data_root(base_path, config.get("WORK_PATH"))
            append_catalog_entry(config.get("WORK_PATH") or "", dr, save_entry)
        except Exception as ce:
            logger.warning("note_catalog append failed: %s", ce)

    # 8. Append to video_metadata_live.jsonl (DATA_ROOT)
    dr = config.get("DATA_ROOT")
    if not dr:
        from config import resolve_data_root
        dr = resolve_data_root(base_path, config.get("WORK_PATH"))
    jsonl_path = os.path.join(dr, "video_metadata_live.jsonl")
    stt.append_video_metadata_jsonl(
        jsonl_path, upload_date or "", vid, v_date, "auto_subs", md_path or "", has_yid=True
    )

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Retry small summary files with auto subs + chunk processing")
    parser.add_argument("--base-path", type=str, default=None)
    parser.add_argument("--size-limit", type=float, default=1.0, help="KB. Files under this size (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="List VIDs only, no processing")
    args = parser.parse_args()

    base = args.base_path
    if not base or not os.path.isdir(base):
        base = os.getenv("BASE_PATH", "").strip() or DEFAULT_BASE
    base = os.path.abspath(base)
    summary_dir = os.path.join(base, "output_new", "summary")
    size_limit_bytes = int(args.size_limit * 1024)

    pairs = find_small_summary_files(summary_dir, size_limit_bytes)
    # Dedupe by vid: keep first (path, vid) per vid so we process each vid once
    seen = set()
    deduped = []
    for p, v in pairs:
        if v not in seen:
            seen.add(v)
            deduped.append((p, v))
    logger.info("Found %d small files -> %d unique VIDs", len(pairs), len(deduped))
    for p, v in deduped[:20]:
        logger.info("  %s (%d B) -> %s", os.path.basename(p), os.path.getsize(p), v)
    if len(deduped) > 20:
        logger.info("  ... and %d more", len(deduped) - 20)

    if not deduped:
        logger.info("No files to process.")
        return

    if args.dry_run:
        logger.info("Dry-run: would process %d VIDs (summary overwrite, MD overwrite if exists)", len(deduped))
        return

    # Load config and OpenAI client
    try:
        from main import load_config
        config = load_config()
    except Exception as e:
        logger.warning("main.load_config failed: %s. Using minimal config.", e)
        try:
            from config import get_config_dict, resolve_data_root
        except ImportError:
            get_config_dict = None  # type: ignore
            resolve_data_root = lambda bp, wp=None: os.path.abspath(bp)  # type: ignore
        config = {"BASE_PATH": base, "WORK_PATH": None, "OUTPUT_MD_PATH": os.getenv("OUTPUT_MD_PATH", "")}
        config["DATA_ROOT"] = resolve_data_root(base, os.getenv("WORK_PATH", "").strip() or None)
        if get_config_dict:
            try:
                config.update(get_config_dict())
            except Exception:
                pass

    from main import initialize_clients
    openai_client, _, main_llm = initialize_clients(config)

    success = 0
    wait_sec = config.get("MIN_WAIT_BETWEEN_VIDEOS", 30)
    for i, (summary_path, vid) in enumerate(deduped):
        if i > 0:
            logger.info("Waiting %ds before next video (rate limit)...", wait_sec)
            time.sleep(wait_sec)
        try:
            if process_one_vid(vid, base, config, openai_client, main_llm, dry_run=False, summary_target_path=summary_path):
                success += 1
        except Exception as e:
            logger.exception("Failed %s: %s", vid, e)
    logger.info("Done. %d/%d succeeded.", success, len(deduped))


if __name__ == "__main__":
    main()
