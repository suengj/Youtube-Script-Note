from __future__ import annotations

import hashlib
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import p03_processed_video_backup as backup  # noqa: E402


def test_backup_is_once_per_kst_day_and_readback_matches_sha256(tmp_path):
    data = tmp_path / "data"
    drive = tmp_path / "drive"
    data.mkdir()
    drive.mkdir()
    source = data / "processed_videos.csv"
    source.write_bytes(b"video_id,first_seen_at\nabcdefghijk,now\n")

    first = backup.backup_processed_registry(data, drive_root=drive, today=date(2026, 10, 23))
    before = first.stat().st_ino
    second = backup.backup_processed_registry(data, drive_root=drive, today=date(2026, 10, 23))

    assert first == second
    assert first.read_bytes() == source.read_bytes()
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(source.read_bytes()).hexdigest()
    assert first.stat().st_ino == before
    assert len(list((drive / backup.BACKUP_DIR).glob("processed_videos_*.csv"))) == 1


def test_backup_retention_keeps_latest_14_exactly_in_target_folder(tmp_path):
    data = tmp_path / "data"
    drive = tmp_path / "drive"
    data.mkdir()
    drive.mkdir()
    (data / "processed_videos.csv").write_text("header\nrow\n")
    folder = drive / backup.BACKUP_DIR
    folder.mkdir(parents=True)
    start = date(2026, 10, 1)
    for offset in range(16):
        stamp = (start + timedelta(days=offset)).strftime("%Y%m%d")
        (folder / f"processed_videos_{stamp}.csv").write_text(f"{offset}\n")
    unrelated = folder / "keep-me.txt"
    unrelated.write_text("unrelated")
    outside = drive / "processed_videos_20200101.csv"
    outside.write_text("outside")

    backup.backup_processed_registry(data, drive_root=drive, today=date(2026, 10, 23))

    names = {path.name for path in folder.glob("processed_videos_*.csv")}
    assert len(names) == 14
    assert "processed_videos_20261023.csv" in names
    assert "processed_videos_20261016.csv" in names
    assert "processed_videos_20261001.csv" not in names
    assert unrelated.exists() and outside.exists()


def test_backup_readback_mismatch_is_warning_and_nonfatal(tmp_path, monkeypatch, caplog):
    data = tmp_path / "data"
    drive = tmp_path / "drive"
    data.mkdir()
    drive.mkdir()
    (data / "processed_videos.csv").write_bytes(b"expected")
    real_replace = backup.os.replace

    def corrupt_before_replace(source, destination):
        Path(source).write_bytes(b"corrupt")
        real_replace(source, destination)

    monkeypatch.setattr(backup.os, "replace", corrupt_before_replace)
    with caplog.at_level(logging.WARNING):
        result = backup.backup_processed_registry(data, drive_root=drive, today=date(2026, 10, 23))

    assert result is None
    assert "SHA-256 readback mismatch" in caplog.text
