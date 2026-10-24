"""Drive API storage for the canonical JEV classification index."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from .classify import classify_if_needed, classify_enabled
from .index import INDEX_FILENAME, cache_key, empty_index, load_index, serialize, validate
from .taxonomy import TAXONOMY_VERSION

logger = logging.getLogger(__name__)
INDEX_FILE_ID = "1KESOin7YU1QR0KSfoKIxhbkytTvQ1t2x"
PHASE_MAX_MIN_ENV = "P03_JEV_PHASE_MAX_MIN"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bytes(value) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


class _DeadlineExpired(TimeoutError):
    pass


def _execute(request, deadline=None):
    """Bound a Drive request by the phase deadline when its HTTP transport allows it."""
    http = getattr(request, "http", None)
    previous_timeout = getattr(http, "timeout", None) if http is not None else None
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExpired("JEV phase deadline reached")
        if http is not None and previous_timeout is not None:
            http.timeout = min(previous_timeout, remaining)
    try:
        result = request.execute()
    finally:
        if http is not None and previous_timeout is not None:
            http.timeout = previous_timeout
    if deadline is not None and time.monotonic() >= deadline:
        raise _DeadlineExpired("JEV phase deadline reached")
    return result


def _meta(drive, file_id, deadline=None):
    request = drive.files().get(fileId=file_id,
        fields="id,name,mimeType,parents,headRevisionId,modifiedTime,md5Checksum",
        supportsAllDrives=True)
    return _execute(request, deadline)


def _download(drive, file_id, deadline=None):
    meta = _meta(drive, file_id, deadline)
    if meta.get("id") != file_id or meta.get("name") != INDEX_FILENAME:
        raise RuntimeError("JEV index Drive identity mismatch")
    raw = _bytes(_execute(drive.files().get_media(fileId=file_id), deadline))
    if hashlib.md5(raw).hexdigest() != meta.get("md5Checksum"):
        raise RuntimeError("JEV index Drive checksum mismatch")
    payload = json.loads(raw.decode("utf-8"))
    validate(payload)
    return meta, raw, payload


def _signature(meta):
    return (str(meta.get("headRevisionId") or ""), str(meta.get("modifiedTime") or ""))


def _upload(drive, file_id, name, parent_id, raw, *, create=False, deadline=None):
    from googleapiclient.http import MediaIoBaseUpload
    import io

    media = MediaIoBaseUpload(io.BytesIO(raw), mimetype="application/json", resumable=False)
    if create:
        return _execute(drive.files().create(body={"name": name, "mimeType": "application/json",
                "parents": [parent_id]}, media_body=media, fields="id", supportsAllDrives=True), deadline)["id"]
    _execute(drive.files().update(fileId=file_id, body={"name": name}, media_body=media,
        fields="id", supportsAllDrives=True), deadline)
    return file_id


def _backup(drive, meta, raw, deadline=None):
    name = INDEX_FILENAME + ".prev"
    parent_id = (meta.get("parents") or [""])[0]
    q = f"'{parent_id}' in parents and name='{name}' and trashed=false"
    matches = _execute(drive.files().list(q=q, fields="files(id,name)", supportsAllDrives=True,
        includeItemsFromAllDrives=True), deadline).get("files", [])
    if matches:
        backup_id = _upload(drive, matches[0]["id"], name, parent_id, raw, deadline=deadline)
    else:
        backup_id = _upload(drive, "", name, parent_id, raw, create=True, deadline=deadline)
    backup_meta = _meta(drive, backup_id, deadline)
    readback = _bytes(_execute(drive.files().get_media(fileId=backup_id), deadline))
    if (backup_meta.get("name") != name or parent_id not in backup_meta.get("parents", []) or
            hashlib.md5(readback).hexdigest() != backup_meta.get("md5Checksum") or _sha(readback) != _sha(raw)):
        raise RuntimeError("JEV index .prev backup verification failed")


def _write_merge(drive, base_meta, original_raw, original, additions, *, attempts=3, deadline=None):
    """Merge additions over the latest Drive index and update the fixed file ID."""
    for _ in range(attempts):
        latest = _meta(drive, INDEX_FILE_ID, deadline)
        if _signature(latest) != _signature(base_meta):
            base_meta, original_raw, original = _download(drive, INDEX_FILE_ID, deadline)
        merged = dict(original)
        merged["entries"] = dict(original["entries"])
        for vid, entry in additions.items():
            merged["entries"][vid] = entry
        candidate = serialize(merged)
        validate(merged)
        check = _meta(drive, INDEX_FILE_ID, deadline)
        if _signature(check) != _signature(base_meta):
            base_meta, original_raw, original = _download(drive, INDEX_FILE_ID, deadline)
            continue
        _backup(drive, base_meta, original_raw, deadline)
        latest = _meta(drive, INDEX_FILE_ID, deadline)
        if _signature(latest) != _signature(base_meta):
            base_meta, original_raw, original = _download(drive, INDEX_FILE_ID, deadline)
            continue
        if deadline is not None and time.monotonic() >= deadline:
            raise _DeadlineExpired("JEV phase deadline reached before index upload")
        _upload(drive, INDEX_FILE_ID, INDEX_FILENAME, "", candidate, deadline=deadline)
        read_meta, readback, _ = _download(drive, INDEX_FILE_ID, deadline)
        if read_meta.get("id") != INDEX_FILE_ID or _sha(readback) != _sha(candidate):
            raise RuntimeError("JEV index SHA-256 readback mismatch")
        return read_meta, _sha(readback)
    raise RuntimeError("JEV index changed during all 3 update attempts")


def _local_index(raw):
    temp = tempfile.TemporaryDirectory(prefix="p03-jev-drive-")
    path = Path(temp.name) / INDEX_FILENAME
    path.write_bytes(raw)
    return temp, path


def _classify_bytes(raw, name, file_id, index_path, provider=None, deadline=None):
    if deadline is not None and time.monotonic() >= deadline:
        raise _DeadlineExpired("JEV phase deadline reached before classification")
    path = index_path.parent / name
    path.write_bytes(raw)
    kwargs = {}
    if deadline is not None and provider is None:
        def bounded_provider():
            from .classify import get_provider
            value = get_provider()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _DeadlineExpired("JEV phase deadline reached before provider call")
            if hasattr(value, "timeout"):
                value.timeout = min(value.timeout, remaining)
            return value
        kwargs["provider_factory"] = bounded_provider
    elif provider is not None and deadline is not None and hasattr(provider, "timeout"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExpired("JEV phase deadline reached before provider call")
        provider.timeout = min(provider.timeout, remaining)
    return classify_if_needed(path, index_path, drive_file_id=file_id, provider=provider, **kwargs)


def _valid_entry(entry, raw):
    if not isinstance(entry, dict) or entry.get("error") or entry.get("stale"):
        return False
    try:
        validate({**empty_index(), "entries": {entry["video_id"]: entry}})
        from .jev import JEV_MODEL, rubric_hash
        expected = cache_key(_sha(raw), TAXONOMY_VERSION, "jev", JEV_MODEL,
                             f"jev-rubric-{rubric_hash()}")
        return entry.get("content_hash") == _sha(raw) and entry.get("cache_key") == expected
    except Exception:
        return False


def _defer_upload(index, file_id, name, raw):
    """Persist a capped upload as a retryable entry in the existing index schema."""
    from datetime import datetime, timezone
    from .classify import resolve_identity
    from .jev import JEV_MODEL, JEV_SCORE_SEMANTICS
    from .taxonomy import TOPIC_IDS

    vid, source_url = resolve_identity(raw)
    previous = index["entries"].get(vid)
    if _valid_entry(previous, raw):
        return
    lineage = dict(previous.get("lineage") or {}) if isinstance(previous, dict) else {}
    lineage["drive_name"] = name
    ids = list(lineage.get("drive_file_ids") or [])
    if file_id not in ids:
        ids.append(file_id)
    lineage["drive_file_ids"] = ids
    index["entries"][vid] = {
        "video_id": vid, "source_url": source_url, "content_hash": _sha(raw),
        "category_scores": (previous.get("category_scores") if isinstance(previous, dict)
                            else {topic: 0 for topic in TOPIC_IDS}),
        "reason": previous.get("reason", "") if isinstance(previous, dict) else "Classification deferred.",
        "score_semantics": (previous.get("score_semantics") if isinstance(previous, dict)
                            else JEV_SCORE_SEMANTICS),
        "taxonomy_version": TAXONOMY_VERSION,
        "classifier": previous.get("classifier") if isinstance(previous, dict) else "jev",
        "model": previous.get("model") if isinstance(previous, dict) else JEV_MODEL,
        "prompt_version": previous.get("prompt_version") if isinstance(previous, dict) else "deferred",
        "classified_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "stale": True, "error": "Deferred by JEV phase time cap", "cache_key": None, "lineage": lineage,
    }


def _run(drive, uploads, *, retries=True, max_calls=None, deadline=None,
         classify_deadline=None, provider=None):
    if deadline is not None and time.monotonic() >= deadline:
        return {"calls": 0, "ok": 0, "errors": 0, "skipped_cached": 0,
                "entries_written": 0, "processed_sources": 0,
                "index_file_id": INDEX_FILE_ID, "sha256": ""}
    base_meta, original, index = _download(drive, INDEX_FILE_ID, deadline)
    classify_deadline = classify_deadline if classify_deadline is not None else deadline
    temp, index_path = _local_index(original)
    calls = ok = errors = cached = visited = 0
    processed = set()
    upload_pos = 0
    try:
        for upload_pos, (file_id, name, raw) in enumerate(uploads):
            if classify_deadline is not None and time.monotonic() >= classify_deadline:
                break
            if max_calls is not None and calls >= max_calls:
                break
            try:
                outcome = _classify_bytes(raw, name, file_id, index_path, provider, classify_deadline)
            except _DeadlineExpired:
                break
            visited += 1
            if outcome.video_id:
                processed.add(outcome.video_id)
            calls += outcome.provider_calls
            ok += outcome.action == "classified"
            cached += outcome.action == "cached"
            errors += outcome.action in {"failed", "unresolved", "read_error"}
        if retries:
            todo = [e for e in index["entries"].values() if isinstance(e, dict) and e.get("error")]
            for entry in sorted(todo, key=lambda e: e.get("video_id", ""))[:20]:
                if entry.get("video_id") in processed:
                    continue
                if classify_deadline is not None and time.monotonic() >= classify_deadline:
                    break
                if max_calls is not None and calls >= max_calls:
                    break
                lineage = entry.get("lineage") or {}
                ids = lineage.get("drive_file_ids") or []
                fid = ids[-1] if ids else ""
                if not fid:
                    continue
                meta = _meta(drive, fid, deadline)
                raw = _bytes(_execute(drive.files().get_media(fileId=fid), deadline))
                if classify_deadline is not None and time.monotonic() >= classify_deadline:
                    break
                try:
                    outcome = _classify_bytes(raw, meta.get("name") or "retry.md", fid, index_path, provider, classify_deadline)
                except _DeadlineExpired:
                    break
                visited += 1
                if outcome.video_id:
                    processed.add(outcome.video_id)
                calls += outcome.provider_calls
                ok += outcome.action == "classified"
                cached += outcome.action == "cached"
                errors += outcome.action in {"failed", "unresolved", "read_error"}
        if retries and classify_deadline is not None and time.monotonic() >= classify_deadline:
            pending = json.loads(index_path.read_text(encoding="utf-8"))
            start = min(upload_pos, len(uploads))
            for file_id, name, raw in uploads[start:]:
                try:
                    _defer_upload(pending, file_id, name, raw)
                except Exception:
                    continue
            index_path.write_bytes(serialize(pending))
        local_index, _ = load_index(index_path)
        base_entries = index.get("entries", {})
        additions = {vid: entry for vid, entry in local_index["entries"].items()
                     if base_entries.get(vid) != entry}
        sha = _sha(original)
        if additions:
            _, sha = _write_merge(drive, base_meta, original, index, additions, deadline=deadline)
        return {"calls": calls, "ok": ok, "errors": errors, "skipped_cached": cached,
                "entries_written": len(additions), "processed_sources": visited,
                "index_file_id": INDEX_FILE_ID, "sha256": sha}
    finally:
        temp.cleanup()


def classify_batch(uploads, *, drive=None, provider=None):
    """Non-fatal end-of-batch hook. Upload rows are (Drive ID, name, summary bytes)."""
    if not classify_enabled():
        return None
    try:
        if drive is None:
            from drive_api_sink import DriveApiSink
            drive = DriveApiSink()._drive
        minutes = max(0.0, float(os.getenv(PHASE_MAX_MIN_ENV, "10")))
        deadline = time.monotonic() + minutes * 60
        reserve = min(45.0, minutes * 60 * 0.15)
        report = _run(drive, uploads, deadline=deadline,
                      classify_deadline=deadline - reserve, provider=provider)
        from . import classify as classify_module
        usage = getattr(classify_module._PROVIDER, "usage", {}) or {}
        token_usage = {key: usage.get(key, 0) for key in ("input_tokens", "output_tokens")}
        logger.info("JEV Drive batch: calls=%s ok=%s errors=%s skipped_cached=%s tokens=%s index_id=%s sha256=%s",
                    report["calls"], report["ok"], report["errors"], report["skipped_cached"],
                    token_usage,
                    report["index_file_id"], report["sha256"])
        return report
    except Exception as exc:
        logger.warning("JEV Drive batch failed (non-fatal): %s", type(exc).__name__)
        return None


def backfill_drive(*, dry_run=False, max_calls=None, all_sources=False, drive=None, provider=None):
    if not all_sources and max_calls is None:
        raise ValueError("backfill-drive requires --max-calls unless --all is set")
    if max_calls is not None and max_calls < 1:
        raise ValueError("--max-calls must be positive")
    if drive is None:
        from drive_api_sink import DriveApiSink
        sink = DriveApiSink()
        drive = sink._drive
        manifest = sink.read_manifest()
    else:
        from drive_api_sink import DriveApiSink
        manifest = DriveApiSink(service=drive).read_manifest()
    _, index_raw, index = _download(drive, INDEX_FILE_ID)
    work = []
    date_by_id = {}
    already = unresolved = 0
    for item in manifest.get("items", []):
        fid = str(item.get("drive_file_id") or "")
        if not fid:
            unresolved += 1
            continue
        date_by_id[fid] = str(item.get("content_revision_at") or item.get("finalized_at") or
                               item.get("recovered_at") or item.get("date") or "9999-99-99")
        meta = _meta(drive, fid)
        raw = _bytes(drive.files().get_media(fileId=fid).execute())
        with tempfile.NamedTemporaryFile() as f:
            f.write(raw); f.flush()
            try:
                from .classify import resolve_identity
                vid, _ = resolve_identity(raw)
            except Exception:
                unresolved += 1
                continue
        if _valid_entry(index["entries"].get(vid), raw):
            already += 1
        else:
            work.append((fid, meta.get("name") or item.get("file") or f"{vid}.md", raw))
    work.sort(key=lambda row: (date_by_id.get(row[0], "9999-99-99"), row[1]))
    from .jev import JEV_MODEL, rubric_hash
    prompt = f"jev-rubric-{rubric_hash()}"
    planned_keys = {}
    planned_calls = 0
    for _, _, raw in work:
        from .classify import resolve_identity
        vid, _ = resolve_identity(raw)
        key = cache_key(_sha(raw), TAXONOMY_VERSION, "jev", JEV_MODEL, prompt)
        current = planned_keys.get(vid, (index["entries"].get(vid) or {}).get("cache_key"))
        if current != key:
            planned_calls += 1
        planned_keys[vid] = key
    if dry_run:
        print(json.dumps({"would_call": min(planned_calls, max_calls or planned_calls),
                          "already_valid": already, "unresolved": unresolved}, sort_keys=True))
        return {"would_call": min(planned_calls, max_calls or planned_calls),
                "already_valid": already, "unresolved": unresolved}
    limit = max_calls if max_calls is not None else len(work)
    calls = total = 0
    for start in range(0, len(work), 100):
        chunk = work[start:start + 100]
        report = _run(drive, chunk, retries=False, max_calls=limit - calls, provider=provider)
        calls += report["calls"]
        total += report["processed_sources"]
        if calls >= limit:
            break
    return {"would_call": min(planned_calls, limit), "processed": total, "calls": calls,
            "already_valid": already, "unresolved": unresolved, "index_file_id": INDEX_FILE_ID}
