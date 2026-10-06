"""classification-index.json storage: load, cache key, locked in-place rewrite with a .prev backup.

The index lives next to manifest.yaml in the Drive Desktop YT_summary root and is a derived
artifact: it never replaces manifest.yaml (source lifecycle SSOT) and the classifier never
writes source Markdown or manifest.yaml.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .taxonomy import TAXONOMY_VERSION

SCHEMA_VERSION = 2
INDEX_FILENAME = "classification-index.json"
OWNER = "suengj/Youtube-Script-Note"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "classification-index.schema.json"

_THREAD_LOCK = threading.Lock()


class IndexError_(RuntimeError):
    """Unreadable index, revision conflict or failed write (previous index restored)."""


def cache_key(content_hash: str, taxonomy_version: str, provider_name: str, model: str, prompt_version: str) -> str:
    # Identical to intelligence-library _cache_key so migrated entries stay cache-valid.
    payload = [content_hash, taxonomy_version, provider_name, model, prompt_version]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def empty_index() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "owner": OWNER, "taxonomy_version": TAXONOMY_VERSION, "entries": {}}


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".prev")


def revision(raw: bytes | None) -> str:
    return hashlib.sha256(raw if raw is not None else b"<missing-index>").hexdigest()


def load_index(path: Path) -> tuple[dict[str, Any], bytes | None]:
    if not path.exists():
        return empty_index(), None
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        hint = f"; previous copy at {backup_path(path)}" if backup_path(path).exists() else ""
        raise IndexError_(f"cannot read classification index: {path}{hint}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        raise IndexError_("classification index must contain an entries object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise IndexError_(
            f"classification index schema_version {payload.get('schema_version')!r} != {SCHEMA_VERSION}; "
            "run `python -m scripts.jev_classify migrate` first"
        )
    return payload, raw


def serialize(index: dict[str, Any]) -> bytes:
    return (json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _rewrite_in_place(path: Path, data: bytes, previous: bytes) -> None:
    """Keep the inode (Drive Desktop does not re-sync a replaced inode); .prev is durable first."""
    backup = backup_path(path)
    fd, temp_name = tempfile.mkstemp(prefix=f".{backup.name}.", suffix=".tmp", dir=path.parent)
    with os.fdopen(fd, "wb") as stream:
        stream.write(previous)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_name, backup)
    try:
        with path.open("r+b") as stream:
            stream.seek(0)
            stream.write(data)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != data:
            raise OSError("index read-back mismatch")
    except BaseException as exc:
        try:
            with path.open("wb") as stream:
                stream.write(backup.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise IndexError_(f"classification index write failed; restore manually from {backup}") from None
        if not isinstance(exc, Exception):
            raise
        raise IndexError_("classification index write failed; previous index restored") from None


def _write_locked(path: Path, data: bytes, current: bytes | None) -> None:
    if current is None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    else:
        _rewrite_in_place(path, data, current)


def update_index(path: Path, mutate: Callable[[dict[str, Any]], bool], *, allow_schema_v1: bool = False) -> bool:
    """Single-writer read-modify-write: thread lock + flock, fresh load, mutate, write if changed.

    Provider calls happen outside this function, so the lock is held only for local I/O.
    Returns True when the index bytes were rewritten.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _THREAD_LOCK, lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if allow_schema_v1:
            current = path.read_bytes() if path.exists() else None
            index = json.loads(current.decode("utf-8")) if current is not None else empty_index()
        else:
            index, current = load_index(path)
        if not mutate(index):
            return False
        data = serialize(index)
        if current is not None and data == current:
            return False
        _write_locked(path, data, current)
        return True


def validate(index: dict[str, Any]) -> None:
    """jsonschema validation against the canonical P03 schema (tests / migration only)."""
    from jsonschema import Draft202012Validator

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(index)
