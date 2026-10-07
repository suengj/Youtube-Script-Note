#!/usr/bin/env python3
"""Import hash-verified BroMath legacy transcripts through the normal P03 summary/publish path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from openai import OpenAI

from scripts.drive_yt_summary.publish import publish_final_md, staging_root
from scripts.md_mobile_utils import assemble_mobile_md, build_save_entry, prepare_mobile_body
from filename_utils import fit_filename

IMPORT_STATUSES = {"LEGACY_UNMATCHED_IMPORT", "LEGACY_MATCHED_IMPORT"}


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"published": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("legacy import state is unreadable") from exc
    if not isinstance(value, dict) or not isinstance(value.get("published"), dict):
        raise ValueError("legacy import state must contain a published object")
    return value


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _resolve_legacy_txt(root: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    resolved.relative_to(root.resolve())
    return resolved


def _safe_stem(stem: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", stem.strip(), flags=re.UNICODE).strip("._")
    return value[:140] or "legacy"


def _summary_counts() -> dict[str, int]:
    return {
        "imported": 0,
        "skipped_existing": 0,
        "skipped_hash_mismatch": 0,
        "failed": 0,
        "stt_runs": 0,
        "obsidian_writes": 0,
    }


def import_reconciliation(
    reconciliation_path: Path,
    legacy_root: Path,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    only: set[str] | None = None,
) -> dict[str, int]:
    """Process eligible manifest items; dependencies remain the same as main.py."""
    counts = _summary_counts()
    try:
        reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
        items = reconciliation["items"]
        if not isinstance(items, list):
            raise ValueError("reconciliation items must be a list")
    except Exception:
        counts["failed"] += 1
        return counts

    candidates = [item for item in items if isinstance(item, dict)
                  and item.get("status") in IMPORT_STATUSES]
    if only is not None:
        candidates = [item for item in candidates if str(item.get("sha256_12", "")) in only]
    if limit is not None:
        candidates = candidates[:max(0, limit)]

    config = None
    client = None
    main_module = None
    state_path = None
    state = None
    if not dry_run:
        try:
            import main as main_module

            config = main_module.load_config()
            client = OpenAI(api_key=config["OPENAI_API_KEY"])
            state_path = Path(config.get("WORK_PATH") or config.get("BASE_PATH") or _REPO_ROOT) / "bromath_legacy_state.json"
            state = _load_state(state_path)
        except Exception:
            counts["failed"] += len(candidates)
            return counts

    for item in candidates:
        sha12 = str(item.get("sha256_12") or "").strip().lower()
        try:
            if not re.fullmatch(r"[0-9a-f]{12}", sha12):
                counts["skipped_hash_mismatch"] += 1
                continue
            source_path = _resolve_legacy_txt(legacy_root, str(item.get("txt") or ""))
            raw_text = source_path.read_bytes()
            text = raw_text.decode("utf-8-sig")
            actual = hashlib.sha256(raw_text).hexdigest()[:12]
            if actual != sha12:
                counts["skipped_hash_mismatch"] += 1
                continue
            if dry_run:
                continue

            video_id = str(item.get("video_id") or "").strip() if item.get("status") == "LEGACY_MATCHED_IMPORT" else ""
            if item.get("status") == "LEGACY_MATCHED_IMPORT" and not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                counts["failed"] += 1
                continue
            if sha12 in state["published"]:
                counts["skipped_existing"] += 1
                continue
            filename = source_path.stem
            response, _usage = main_module.run_direct_summary(client, text, filename, video_id, config)
            body, _summary_tags, title, tldr = prepare_mobile_body(response)
            title = title or filename
            safe_name = _safe_stem(str(item.get("stem") or source_path.stem))
            output_name = fit_filename(f"{safe_name}_legacy-{sha12}.md", video_id)
            rel_path = f"legacy_import/{output_name}"
            base_path = config.get("BASE_PATH") or str(_REPO_ROOT)
            work_path = config.get("WORK_PATH") or ""
            # Give publish a local non-vault fallback root; mirror=False remains explicit.
            local_md_root = str(Path(work_path or base_path) / "bromath_legacy_publish_local")
            md_abs_path = str(Path(local_md_root) / rel_path)
            entry = build_save_entry(
                md_abs_path=md_abs_path,
                md_root=local_md_root,
                vid=video_id,
                channel="BroMath",
                upload_date="",
                transcript_date="",
                lang="ko",
                suffix="legacy",
                source_url=f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
                tags=["bromath"],
                title=title,
                tldr=tldr,
            )
            entry["vid"] = video_id
            entry["source_url"] = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
            entry["source"] = "legacy_import"
            content = assemble_mobile_md(entry, body)
            content = content.replace("---\n", "---\nsource: legacy_import\n", 1)
            root = staging_root(work_path, base_path)
            staging_file = root / rel_path
            staging_file.parent.mkdir(parents=True, exist_ok=True)
            staging_file.write_text(content, encoding="utf-8-sig")
            published = publish_final_md(
                rel_path=rel_path,
                content=content,
                staging_path=str(staging_file),
                md_root=local_md_root,
                mirror=False,
                base_path=base_path,
                work_path=work_path,
                video_id=video_id,
                title=title,
            )
            if not published.drive_ok:
                counts["failed"] += 1
                continue
            state["published"][sha12] = {
                "rel_path": rel_path,
                "video_id": video_id,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
            _save_state(state_path, state)
            counts["imported"] += 1
        except Exception:
            counts["failed"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reconciliation_json", type=Path)
    parser.add_argument("--legacy-root", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only", help="comma-separated sha256_12 values")
    args = parser.parse_args(argv)
    only = {part.strip().lower() for part in args.only.split(",") if part.strip()} if args.only else None
    counts = import_reconciliation(
        args.reconciliation_json.resolve(), args.legacy_root.resolve(),
        dry_run=args.dry_run, limit=args.limit, only=only,
    )
    print(json.dumps(counts, sort_keys=True))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
