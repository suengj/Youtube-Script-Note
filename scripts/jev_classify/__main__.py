"""Operator CLI (never part of the daily path except where noted).

  python -m scripts.jev_classify migrate [--index PATH] [--dry-run]
  python -m scripts.jev_classify classify (--path MD ... | --date YYYY-MM-DD | --since D [--until D] | --all)
                                          [--force] [--dry-run]
  python -m scripts.jev_classify retry-errors [--limit N]

`classify` is the explicit backfill/reclassify command: sources come from P03's own Drive sync
state (date folder of relative_path), never from a Drive crawl. Without --force only new,
changed, previously failed or rubric/model/taxonomy-invalidated sources call JEV.
The JEV key is read from the file named by JEV_API_KEY_FILE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .classify import UnresolvedSource, classify_paths, resolve_identity, retry_errors
from .index import INDEX_FILENAME, cache_key, load_index
from .taxonomy import TAXONOMY_VERSION


def _sync_root(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    from scripts.drive_yt_summary.config import discover_yt_summary_root

    root = discover_yt_summary_root()
    if root is None:
        raise SystemExit("YT_summary root not found; pass --sync-root or set P03_DRIVE_SYNC_ROOT")
    return root


def _state_sources(args: argparse.Namespace) -> list[Path]:
    from scripts.drive_yt_summary.config import load_config
    from scripts.drive_yt_summary.state import load_state

    state = load_state(load_config(sync_root=args.sync_root).state_path)
    out = []
    for rel, entry in sorted(state.files.items()):
        folder = rel.split("/", 1)[0].replace("_", "-")
        if args.date and folder != args.date:
            continue
        if args.since and folder < args.since:
            continue
        if args.until and folder > args.until:
            continue
        out.append(Path(entry.dest_path))
    return out


def _plan(paths: list[Path], index_path: Path, force: bool) -> dict:
    from .jev import JEV_MODEL, rubric_hash

    index, _ = load_index(index_path) if index_path.exists() else ({"entries": {}}, None)
    prompt = f"jev-rubric-{rubric_hash()}"
    would = unresolved = missing = 0
    for path in paths:
        try:
            raw = path.read_bytes()
            vid, _ = resolve_identity(raw)
        except UnresolvedSource:
            unresolved += 1
            continue
        except OSError:
            missing += 1
            continue
        prev = index["entries"].get(vid) or {}
        key = cache_key(hashlib.sha256(raw).hexdigest(), TAXONOMY_VERSION, "jev", JEV_MODEL, prompt)
        if force or prev.get("error") or prev.get("cache_key") != key:
            would += 1
    return {"dry_run": True, "sources": len(paths), "would_call": would, "unresolved": unresolved, "missing": missing}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.jev_classify", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sync-root", help="Drive Desktop YT_summary folder (default: auto-discover)")
    parser.add_argument("--index", help=f"index path (default: <sync-root>/{INDEX_FILENAME})")
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate", help="rewrite the v1 index to P03 video_id identity (no JEV calls)")
    m.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("classify", help="explicit backfill / reclassify")
    sel = c.add_mutually_exclusive_group(required=True)
    sel.add_argument("--path", nargs="+")
    sel.add_argument("--date")
    sel.add_argument("--since")
    sel.add_argument("--all", action="store_true")
    c.add_argument("--until")
    c.add_argument("--force", action="store_true", help="reclassify even when the cache entry is valid")
    c.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("retry-errors")
    r.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    if args.cmd == "classify" and args.until and not args.since:
        parser.error("--until requires --since")

    sync_root = _sync_root(args.sync_root)
    index_path = Path(args.index).expanduser() if args.index else sync_root / INDEX_FILENAME
    if args.cmd == "migrate":
        from .migrate import migrate_file

        result = migrate_file(index_path, dry_run=args.dry_run)
        ok = not result.get("parity_mismatches") and not result.get("cache_key_mismatches")
    elif args.cmd == "classify":
        args.date = getattr(args, "date", None)
        paths = [Path(p) for p in args.path] if args.path else _state_sources(args)
        if args.dry_run:
            result = _plan(paths, index_path, args.force)
            ok = True
        else:
            summary = classify_paths(paths, index_path, force=args.force)
            result = summary.as_dict()
            ok = "failed" not in summary.counts()
    else:
        summary = retry_errors(index_path, sync_root / "source", limit=args.limit)
        result = summary.as_dict()
        ok = "failed" not in summary.counts()
    from . import classify as _c

    if getattr(_c._PROVIDER, "usage", None) is not None:
        result["provider_usage"] = _c._PROVIDER.usage
    result["index_path"] = str(index_path)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
