#!/usr/bin/env python3
"""Build or merge DATA_ROOT/processed_videos.csv from P03 processing sources."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from processed_video_registry import build_registry  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path(os.getenv("DATA_ROOT", ROOT / "data")))
    parser.add_argument("--no-sheet", action="store_true", help="build from local CSVs without reading Google Sheets")
    parser.add_argument("--dry-run", action="store_true", help="print discovered record counts by source without writing")
    args = parser.parse_args(argv)
    try:
        counts = build_registry(args.data_root, include_sheet=not args.no_sheet, dry_run=args.dry_run)
    except Exception as exc:
        print(f"Processed registry build failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for source, count in counts.items():
        print(f"{source}: {count}")
    print(f"mode: {'dry-run' if args.dry_run else 'written'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
