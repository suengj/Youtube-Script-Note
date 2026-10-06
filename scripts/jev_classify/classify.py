"""classify_if_needed for one P03 source, error retry, and the explicit operator backfill.

Identity is the YouTube video_id resolved from the Markdown front matter (source_url / vid).
Drive file IDs and file names are lineage only. A provider call happens only when there is no
valid entry for (content_hash, taxonomy_version, provider, model, prompt_version), the previous
attempt failed, or the operator forces a reclassify. The source Markdown and manifest.yaml are
only ever read.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .index import INDEX_FILENAME, VIDEO_ID_RE, cache_key, load_index, update_index
from .taxonomy import TAXONOMY_VERSION, TOPIC_IDS

logger = logging.getLogger(__name__)

ENABLED_ENV = "P03_JEV_CLASSIFY_ENABLED"
MAX_BODY_BYTES = 1_048_576
FRONT_MATTER_SCAN_CAP = 65_536
DEFAULT_RETRY_LIMIT = 20

_PROVIDER: Any = None
_PROVIDER_LOCK = threading.Lock()


class UnresolvedSource(ValueError):
    """The Markdown carries no usable YouTube identity (source_url / vid)."""


def classify_enabled() -> bool:
    """P03_JEV_CLASSIFY_ENABLED (default off): the daily path classifies only when the owner enables it."""
    raw = (os.getenv(ENABLED_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _front_matter(raw: bytes) -> dict[str, str]:
    head = raw[:FRONT_MATTER_SCAN_CAP].decode("utf-8-sig", errors="replace")
    if not head.startswith("---"):
        return {}
    close = re.search(r"^---[ \t]*$", head[3:], re.MULTILINE)
    if not close:
        return {}
    fields: dict[str, str] = {}
    for line in head[3:3 + close.start()].splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*?)[ \t]*$", line)
        if match:
            fields[match.group(1)] = match.group(2).strip("\"'")
    return fields


def video_id_from_url(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    candidate = None
    if host.endswith("youtube.com"):
        if parts.path == "/watch":
            candidate = (parse_qs(parts.query).get("v") or [None])[0]
        else:
            m = re.match(r"^/(?:shorts|live|embed)/([^/?#]+)", parts.path)
            candidate = m.group(1) if m else None
    elif host == "youtu.be":
        candidate = parts.path.strip("/").split("/")[0] or None
    return candidate if candidate and VIDEO_ID_RE.match(candidate) else None


def resolve_identity(raw: bytes) -> tuple[str, str]:
    """Return (video_id, canonical source_url) from front matter; raise UnresolvedSource."""
    fields = _front_matter(raw)
    url_vid = video_id_from_url(fields.get("source_url", ""))
    fm_vid = fields.get("vid", "")
    fm_vid = fm_vid if VIDEO_ID_RE.match(fm_vid) else ""
    if url_vid and fm_vid and url_vid != fm_vid:
        raise UnresolvedSource(f"source_url video {url_vid} != vid {fm_vid}")
    vid = url_vid or fm_vid
    if not vid:
        raise UnresolvedSource("front matter has no YouTube source_url or vid")
    return vid, f"https://www.youtube.com/watch?v={vid}"


def get_provider() -> Any:
    """Process-wide JevProvider (key from JEV_API_KEY_FILE); raises JevConfigError when not configured."""
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            from .jev import JevProvider

            _PROVIDER = JevProvider()
        return _PROVIDER


def _lineage(previous: dict | None, drive_name: str, drive_file_id: str | None) -> dict[str, Any]:
    lineage = dict(previous.get("lineage") or {}) if isinstance(previous, dict) else {}
    if drive_name:
        lineage["drive_name"] = drive_name
    ids = list(lineage.get("drive_file_ids") or [])
    if drive_file_id and drive_file_id not in ids:
        ids.append(drive_file_id)
    lineage["drive_file_ids"] = ids
    return lineage


@dataclass
class ClassifyOutcome:
    video_id: str = ""
    action: str = "skipped"  # cached | classified | failed | unresolved | read_error
    provider_calls: int = 0
    error: str = ""


def classify_if_needed(
    md_path: Path,
    index_path: Path,
    *,
    provider: Any = None,
    provider_factory: Callable[[], Any] = get_provider,
    taxonomy_version: str = TAXONOMY_VERSION,
    force: bool = False,
    drive_file_id: str | None = None,
    now: Callable[[], str] = _now,
) -> ClassifyOutcome:
    out = ClassifyOutcome()
    try:
        with md_path.open("rb") as stream:
            raw = stream.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise OSError(f"Markdown exceeds {MAX_BODY_BYTES} bytes")
        out.video_id, source_url = resolve_identity(raw)
    except UnresolvedSource as exc:
        out.action, out.error = "unresolved", str(exc)
        return out
    except OSError as exc:
        out.action, out.error = "read_error", f"{type(exc).__name__}: {exc}"
        return out

    content_hash = hashlib.sha256(raw).hexdigest()
    drive_name = md_path.name
    vid = out.video_id
    index, _raw = load_index(index_path)
    previous = index["entries"].get(vid)

    error: str | None = None
    try:
        provider = provider or provider_factory()
    except Exception as exc:  # missing/invalid key: no call made, entry marked for retry
        provider = None
        error = f"{type(exc).__name__}: {str(exc)[:200]}"

    key = cache_key(content_hash, taxonomy_version, provider.name, provider.model, provider.prompt_version) if provider else None
    if (not force and key and isinstance(previous, dict) and previous.get("cache_key") == key
            and previous.get("content_hash") == content_hash and not previous.get("error")):
        out.action = "cached"
        lineage = _lineage(previous, drive_name, drive_file_id)
        if lineage != previous.get("lineage") or previous.get("source_url") != source_url:
            def touch(idx: dict) -> bool:
                entry = idx["entries"].get(vid)
                if not isinstance(entry, dict) or entry.get("cache_key") != key:
                    return False
                entry["lineage"] = _lineage(entry, drive_name, drive_file_id)
                entry["source_url"] = source_url
                return True
            update_index(index_path, touch)
        return out

    scores = details = reason = None
    if provider is not None:
        out.provider_calls = 1
        try:
            if hasattr(provider, "classify_detail"):
                scores, reason, details = provider.classify_detail(raw.decode("utf-8-sig"))
            else:
                scores, reason = provider.classify(raw.decode("utf-8-sig"))
            _validate_scores(scores)
        except Exception as exc:  # isolated: keep the stale entry, retry on the next run
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            scores = details = reason = None

    from .jev import JEV_MODEL, JEV_SCORE_SEMANTICS

    def apply(idx: dict) -> bool:
        prev = idx["entries"].get(vid)
        cached = isinstance(prev, dict)
        entry = {
            "video_id": vid,
            "source_url": source_url,
            "content_hash": content_hash,
            "category_scores": scores if error is None else (
                prev.get("category_scores") if cached else {t: 0 for t in TOPIC_IDS}),
            "reason": str(reason if error is None else (prev.get("reason", "") if cached else
                          "Classification failed; scores are placeholders."))[:240],
            "score_semantics": getattr(provider, "score_semantics", JEV_SCORE_SEMANTICS),
            "taxonomy_version": taxonomy_version,
            "classifier": provider.name if provider else "jev",
            "model": provider.model if provider else JEV_MODEL,
            "prompt_version": provider.prompt_version if provider else (prev.get("prompt_version", "") if cached else "unconfigured"),
            "classified_at": now() if error is None else (prev.get("classified_at") if cached else now()),
            "stale": error is not None,
            "error": error,
            "cache_key": key if error is None else None,
            "lineage": _lineage(prev, drive_name, drive_file_id),
        }
        if error is None and details is not None:
            entry["details"] = details
        elif error is not None and cached and isinstance(prev.get("details"), dict):
            entry["details"] = prev["details"]
        if error is not None and cached:
            # keep the last good result's provenance so the stale entry stays interpretable
            for k in ("taxonomy_version", "classifier", "model", "prompt_version", "score_semantics"):
                if prev.get(k):
                    entry[k] = prev[k]
            entry["content_hash"] = prev.get("content_hash") or content_hash
        idx["entries"][vid] = entry
        idx["taxonomy_version"] = taxonomy_version
        if provider is not None and error is None:
            idx["classifier_config"] = {"classifier": provider.name, "model": provider.model,
                                        "prompt_version": provider.prompt_version}
        return True

    update_index(index_path, apply)
    out.action = "classified" if error is None else "failed"
    out.error = error or ""
    return out


def _validate_scores(scores: Any) -> None:
    if not isinstance(scores, dict) or set(scores) != set(TOPIC_IDS):
        raise ValueError("provider scores must include the five topic IDs and other_unknown")
    if any(type(v) is not int or not 0 <= v <= 100 for v in scores.values()):
        raise ValueError("provider scores must be independent integers from 0 to 100")


@dataclass
class RunSummary:
    outcomes: list = field(default_factory=list)

    @property
    def provider_calls(self) -> int:
        return sum(o.provider_calls for o in self.outcomes)

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for o in self.outcomes:
            c[o.action] = c.get(o.action, 0) + 1
        return c

    def as_dict(self) -> dict[str, Any]:
        return {"sources": len(self.outcomes), "provider_calls": self.provider_calls, "actions": self.counts(),
                "errors": [f"{o.video_id or '?'}: {o.error}" for o in self.outcomes if o.error][:20]}


def retry_errors(index_path: Path, source_dir: Path, *, limit: int = DEFAULT_RETRY_LIMIT, **kwargs: Any) -> RunSummary:
    """Daily-path retry of entries whose last attempt failed (bounded; never a corpus backfill)."""
    summary = RunSummary()
    if not index_path.exists():
        return summary
    index, _ = load_index(index_path)
    pending = [e for e in index["entries"].values() if isinstance(e, dict) and e.get("error")]
    for entry in sorted(pending, key=lambda e: e.get("video_id", ""))[:limit]:
        name = (entry.get("lineage") or {}).get("drive_name")
        path = source_dir / name if name else None
        if path is None or not path.is_file():
            continue
        summary.outcomes.append(classify_if_needed(path, index_path, **kwargs))
    return summary


def classify_paths(paths: Iterable[Path], index_path: Path, **kwargs: Any) -> RunSummary:
    summary = RunSummary()
    for path in paths:
        summary.outcomes.append(classify_if_needed(Path(path), index_path, **kwargs))
    return summary


def classify_after_publish(drive_path: str, sync_root: Path) -> ClassifyOutcome | None:
    """Publish-path hook: one source, behind P03_JEV_CLASSIFY_ENABLED; never raises."""
    if not classify_enabled():
        return None
    try:
        return classify_if_needed(Path(drive_path), sync_root / INDEX_FILENAME)
    except Exception as exc:  # never break publish
        logger.warning("JEV classify failed (non-fatal) for %s: %s", Path(drive_path).name, type(exc).__name__)
        return ClassifyOutcome(action="failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
