#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VTT/SRT lifecycle: quarantine, deletion, legacy yt_subs cleanup."""

from __future__ import annotations

import glob
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Set

logger = logging.getLogger(__name__)

QUARANTINE_SUBDIR = os.path.join("quarantine", "subtitles")
DEFAULT_QUARANTINE_DAYS = 7
RECENT_PRESERVE_HOURS = 72


@dataclass
class LegacySubsCleanupReport:
    before_count: int = 0
    before_bytes: int = 0
    deleted_count: int = 0
    quarantined_count: int = 0
    preserved_count: int = 0
    deleted_bytes: int = 0
    dry_run: bool = True
    details: List[str] = field(default_factory=list)


def quarantine_root(work_path: str) -> str:
    return os.path.join(work_path, QUARANTINE_SUBDIR)


def move_to_quarantine(
    work_path: str,
    video_id: str,
    source_path: str,
    *,
    reason: str = "",
) -> str:
    """Move a subtitle file into quarantine/subtitles/<video_id>/."""
    dest_dir = os.path.join(quarantine_root(work_path), video_id)
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.basename(source_path)
    dest = os.path.join(dest_dir, base)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(base)
        dest = os.path.join(dest_dir, f"{stem}_{int(time.time())}{ext}")
    shutil.move(source_path, dest)
    meta = os.path.join(dest_dir, "_quarantine_meta.txt")
    with open(meta, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} moved {base} reason={reason}\n")
    logger.info("Quarantined subtitle %s -> %s (%s)", source_path, dest, reason)
    return dest


def quarantine_job_subtitles(work_path: str, video_id: str, paths: List[str], reason: str = "") -> int:
    moved = 0
    for path in paths:
        if path and os.path.isfile(path):
            move_to_quarantine(work_path, video_id, path, reason=reason)
            moved += 1
    return moved


def delete_subtitle_file(path: Optional[str]) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        logger.debug("Deleted subtitle file %s", path)
        return True
    except OSError as exc:
        logger.warning("Failed to delete subtitle %s: %s", path, exc)
        return False


def cleanup_expired_quarantine(work_path: str, *, max_age_days: int = DEFAULT_QUARANTINE_DAYS, dry_run: bool = False) -> int:
    root = quarantine_root(work_path)
    if not os.path.isdir(root):
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for vid in os.listdir(root):
        vid_dir = os.path.join(root, vid)
        if not os.path.isdir(vid_dir):
            continue
        try:
            mtime = os.path.getmtime(vid_dir)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        if dry_run:
            logger.info("[dry-run] Would remove quarantine dir %s", vid_dir)
        else:
            try:
                shutil.rmtree(vid_dir)
            except OSError as exc:
                logger.warning("Failed to remove quarantine %s: %s", vid_dir, exc)
                continue
        removed += 1
    return removed


def _terminal_success_v_ids(output_df_path: str) -> Set[str]:
    try:
        import pandas as pd

        if not os.path.isfile(output_df_path):
            return set()
        df = pd.read_csv(output_df_path, encoding="utf-8-sig")
        if "v_id" not in df.columns or "status" not in df.columns:
            return set()
        ok = df[df["status"].astype(str).isin(["success", "already_existed"])]
        return set(ok["v_id"].astype(str).str.strip())
    except Exception as exc:
        logger.warning("Could not read output_df for legacy cleanup: %s", exc)
        return set()


def _subs_video_id_from_name(name: str) -> Optional[str]:
    # {video_id}.{lang}[-orig].vtt
    if not name.endswith((".vtt", ".srt")):
        return None
    base = name.rsplit(".", 1)[0]
    if "-orig" in base:
        base = base.replace("-orig", "")
    parts = base.split(".")
    if parts:
        return parts[0]
    return None


def inventory_yt_subs(yt_subs_dir: str) -> tuple[int, int]:
    count = 0
    total = 0
    if not os.path.isdir(yt_subs_dir):
        return 0, 0
    for name in os.listdir(yt_subs_dir):
        path = os.path.join(yt_subs_dir, name)
        if os.path.isfile(path) and name.endswith((".vtt", ".srt")):
            count += 1
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
    return count, total


def cleanup_legacy_yt_subs(
    work_path: str,
    yt_subs_dir: str,
    output_full_path: str,
    output_df_path: str,
    output_md_path: str,
    *,
    dry_run: bool = True,
    recent_preserve_hours: int = RECENT_PRESERVE_HOURS,
    find_durable_full: Optional[Callable[[str, str], Optional[str]]] = None,
) -> LegacySubsCleanupReport:
    """
    One-time safe cleanup of legacy yt_subs/ after new pipeline verified.
    """
    from transcript_cache import find_durable_full_transcript

    finder = find_durable_full or find_durable_full_transcript
    report = LegacySubsCleanupReport(dry_run=dry_run)
    report.before_count, report.before_bytes = inventory_yt_subs(yt_subs_dir)
    if not os.path.isdir(yt_subs_dir):
        return report

    success_vids = _terminal_success_v_ids(output_df_path)
    recent_cutoff = time.time() - recent_preserve_hours * 3600
    find_durable = finder

    for name in os.listdir(yt_subs_dir):
        path = os.path.join(yt_subs_dir, name)
        if not os.path.isfile(path) or not name.endswith((".vtt", ".srt")):
            continue
        vid = _subs_video_id_from_name(name)
        try:
            mtime = os.path.getmtime(path)
            size = os.path.getsize(path)
        except OSError:
            report.preserved_count += 1
            continue

        if mtime >= recent_cutoff:
            report.preserved_count += 1
            report.details.append(f"preserve recent: {name}")
            continue

        durable = find_durable(output_full_path, vid) if vid else None
        has_success = vid in success_vids if vid else False

        if durable or has_success:
            action = "delete" if not dry_run else "dry-delete"
            report.details.append(f"{action}: {name} (durable={bool(durable)} success={has_success})")
            if not dry_run:
                try:
                    os.remove(path)
                    report.deleted_count += 1
                    report.deleted_bytes += size
                except OSError as exc:
                    report.preserved_count += 1
                    report.details.append(f"failed delete {name}: {exc}")
            else:
                report.deleted_count += 1
                report.deleted_bytes += size
        else:
            action = "quarantine" if not dry_run else "dry-quarantine"
            report.details.append(f"{action}: {name}")
            if not dry_run and vid:
                try:
                    move_to_quarantine(work_path, vid, path, reason="legacy_unverified")
                    report.quarantined_count += 1
                except OSError:
                    report.preserved_count += 1
            elif dry_run:
                report.quarantined_count += 1
            else:
                report.preserved_count += 1

    return report
