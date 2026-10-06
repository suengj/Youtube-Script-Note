#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plan (and optionally apply) renames for vault notes cut at 255 NFD bytes by storage.

Before the NFD-aware filename budget, iCloud/Obsidian cut long note names at 255 bytes of
their decomposed form, e.g. ``..._JyyAGZ1-r94_ko-orig_auto_subs_.md`` or ``..._Be5b.md``.
The full canonical name usually exists in Drive ``source/`` and in the sync state.

Default mode is a read-only dry run that prints (or writes) a plan. Every candidate
vault note (NFD name 253..255 bytes) gets exactly one row:

  RENAME                      vault_rel -> new_vault_rel (canonical ID + suffix, title
                              shortened with ``filename_utils.fit_filename`` to 240 bytes)
  UNRESOLVED                  with a reason (no / several canonicals, content differs, ...)

Drive ``source/`` files that are truncated duplicates of a canonical file get
``DRIVE_DUP_DELETE_CANDIDATE``. This script never deletes anything.

A vault note matches a canonical only if ALL hold:
  * the note's NFD name is 253..255 bytes;
  * exactly one canonical in the same date folder (sync-state entry whose NFD name is
    > 255 bytes) has an NFD stem of which the note's NFD stem is a strict prefix;
  * the contents are identical (sha256 of the utf-8-sig decoded text, as in sync.py).

``--apply PLAN.json`` renames vault files only, only RENAME rows, never deletes, refuses
when the target exists, re-checks the content hash before each rename, and writes an
undo log (``PLAN.undo.json`` unless ``--undo-log`` is given).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from filename_utils import fit_filename  # noqa: E402
from scripts.drive_yt_summary.state import SyncStateEntry, load_state  # noqa: E402

TRUNC_MIN_BYTES = 253
TRUNC_MAX_BYTES = 255
RENAME_MAX_BYTES = 240

RENAME = "RENAME"
DRIVE_DUP = "DRIVE_DUP_DELETE_CANDIDATE"
UNRESOLVED = "UNRESOLVED"

_DATE_DIR = re.compile(r"^\d{4}_\d{2}_\d{2}$")
_VID_RE = re.compile(r"_([A-Za-z0-9_-]{11})_(?:[a-z]{2,3}(?:-[A-Za-z]+)?(?:_|\.))")


def nfd_bytes(text: str) -> bytes:
    return unicodedata.normalize("NFD", text).encode("utf-8")


def blen(text: str) -> int:
    """max(NFC, NFD) UTF-8 bytes, the same measure fit_filename uses."""
    return max(
        len(unicodedata.normalize("NFC", text).encode("utf-8")),
        len(unicodedata.normalize("NFD", text).encode("utf-8")),
    )


def file_hash(path: Path) -> str:
    """Same content hash as the sync (decoded text without BOM, re-encoded)."""
    return hashlib.sha256(path.read_text(encoding="utf-8-sig").encode("utf-8")).hexdigest()


def is_truncation_candidate(name: str) -> bool:
    return name.endswith(".md") and TRUNC_MIN_BYTES <= len(nfd_bytes(name)) <= TRUNC_MAX_BYTES


@dataclass
class Row:
    action: str
    vault_rel: str = ""
    new_vault_rel: str = ""
    reason: str = ""
    vault_sha256: str = ""
    canonical: str = ""
    canonical_sha256: str = ""
    drive_path: str = ""
    date: str = ""
    extra: Dict[str, object] = field(default_factory=dict)


@dataclass
class Canon:
    key: str  # state key (folder/name)
    name: str  # NFC file name
    nfd: bytes
    entry: SyncStateEntry

    def drive_file(self) -> Path:
        return Path(self.entry.dest_path)

    def content_hash(self) -> str:
        """Hash of the actual Drive file when readable, else the recorded hash."""
        try:
            return file_hash(self.drive_file())
        except (OSError, ValueError):
            return (self.entry.content_hash or "").strip()


def _canon_index(state_files: Dict[str, SyncStateEntry]) -> Dict[str, List[Canon]]:
    by_folder: Dict[str, List[Canon]] = {}
    for key, entry in state_files.items():
        folder, sep, name = key.rpartition("/")
        if not sep or not name.endswith(".md"):
            continue
        nfd = nfd_bytes(name)
        if len(nfd) <= TRUNC_MAX_BYTES:
            continue
        by_folder.setdefault(folder, []).append(
            Canon(key, unicodedata.normalize("NFC", name), nfd, entry)
        )
    return by_folder


def _video_id(canon: Canon) -> str:
    vid = (canon.entry.video_id or "").strip()
    if vid and vid in canon.name:
        return vid
    m = _VID_RE.search(canon.name)
    return m.group(1) if m else ""


def new_name_for(canon: Canon) -> Optional[str]:
    """Canonical name with only the title shortened so it fits 240 bytes (NFC/NFD max)."""
    vid = _video_id(canon)
    if not vid:
        return None
    head = canon.name.split("_", 1)[0]
    prefix = head + "_" if "_" in canon.name else ""
    out = unicodedata.normalize(
        "NFC", fit_filename(canon.name, vid, protect_prefix=prefix, max_bytes=RENAME_MAX_BYTES)
    )
    idx = canon.name.rfind(vid)
    if blen(out) > RENAME_MAX_BYTES or not out.endswith(canon.name[idx:]):
        return None
    return out


def _is_strict_prefix(cand_stem: bytes, canon_nfd: bytes) -> bool:
    stem = canon_nfd[:-3]
    return len(stem) > len(cand_stem) and stem.startswith(cand_stem)


def _plan_vault(vault: Path, canons: Dict[str, List[Canon]], only_date: str) -> List[Row]:
    rows: List[Row] = []
    for day in sorted(os.listdir(vault)):
        day_dir = vault / day
        if not _DATE_DIR.match(day) or not day_dir.is_dir():
            continue
        if only_date and day != only_date:
            continue
        for name in sorted(os.listdir(day_dir)):
            if not is_truncation_candidate(name):
                continue
            rel = f"{day}/{name}"
            path = day_dir / name
            row = Row(UNRESOLVED, vault_rel=rel, date=day)
            stem = nfd_bytes(name)[:-3]
            matches = [c for c in canons.get(day, []) if _is_strict_prefix(stem, c.nfd)]
            if not matches:
                row.reason = "no_canonical_match"
            elif len(matches) > 1:
                row.reason = "ambiguous_canonical: " + " | ".join(sorted(c.name for c in matches))
            else:
                canon = matches[0]
                row.canonical = canon.key
                row.canonical_sha256 = canon.content_hash()
                # Read the vault note only now: unmatched notes are never opened
                # (avoids pulling iCloud placeholders for thousands of files).
                try:
                    row.vault_sha256 = file_hash(path)
                except (OSError, ValueError) as exc:
                    row.vault_sha256 = ""
                    row.reason = f"vault_unreadable: {exc}"
                if row.reason:
                    pass
                elif not row.canonical_sha256:
                    row.reason = "canonical_content_unreadable"
                elif row.canonical_sha256 != row.vault_sha256:
                    row.reason = "content_differs"
                else:
                    new = new_name_for(canon)
                    if new is None:
                        row.reason = "cannot_fit_name_or_no_video_id"
                    else:
                        new_rel = f"{day}/{new}"
                        if os.path.lexists(day_dir / new):
                            row.reason = "target_exists"
                            row.new_vault_rel = new_rel
                        else:
                            row.action, row.new_vault_rel = RENAME, new_rel
            rows.append(row)

    # Two notes must never be planned onto one target.
    targets: Dict[str, List[Row]] = {}
    for r in rows:
        if r.action == RENAME:
            targets.setdefault(unicodedata.normalize("NFC", r.new_vault_rel).casefold(), []).append(r)
    for group in targets.values():
        if len(group) > 1:
            for r in group:
                r.action, r.reason = UNRESOLVED, "duplicate_target"
    return rows


def _plan_drive_dups(source_dir: Path, canons: Dict[str, List[Canon]]) -> List[Row]:
    if not source_dir.is_dir():
        return []
    every = [c for group in canons.values() for c in group]
    rows: List[Row] = []
    for name in sorted(os.listdir(source_dir)):
        if not is_truncation_candidate(name):
            continue
        stem = nfd_bytes(name)[:-3]
        matches = [c for c in every if _is_strict_prefix(stem, c.nfd)]
        if not matches:
            continue
        path = source_dir / name
        row = Row(UNRESOLVED, drive_path=str(path), vault_rel="", reason="")
        try:
            h = file_hash(path)
        except (OSError, ValueError) as exc:
            row.reason = f"drive_unreadable: {exc}"
            rows.append(row)
            continue
        same = [c for c in matches if c.content_hash() == h]
        distinct = {c.key for c in matches}
        if not same:
            row.reason = "drive_dup_content_differs"
        elif len(distinct) > 1 and len(same) != 1:
            row.reason = "drive_dup_ambiguous_canonical"
        else:
            canon = same[0]
            if canon.drive_file().name == name or str(canon.drive_file()) == str(path):
                continue
            row.action, row.canonical, row.canonical_sha256 = DRIVE_DUP, canon.key, h
            row.drive_path = str(path)
            row.extra = {"canonical_drive_path": str(canon.drive_file())}
        row.date = canon_date(row.canonical)
        rows.append(row)
    return rows


def canon_date(key: str) -> str:
    return key.partition("/")[0] if key else ""


def build_plan(vault: Path, drive_root: Path, state_path: Path, only_date: str = "") -> dict:
    state = load_state(state_path)
    canons = _canon_index(state.files)
    rows = _plan_vault(vault, canons, only_date)
    if not only_date:
        rows += _plan_drive_dups(drive_root / "source", canons)
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r.action] = counts.get(r.action, 0) + 1
    return {
        "version": 1,
        "dry_run": True,
        "vault": str(vault),
        "drive_root": str(drive_root),
        "state_path": str(state_path),
        "counts": counts,
        "rows": [asdict(r) for r in rows],
    }


TSV_COLUMNS = ["action", "date", "vault_rel", "new_vault_rel", "drive_path", "canonical", "reason"]


def plan_to_tsv(plan: dict) -> str:
    def cell(v: object) -> str:
        return str(v or "").replace("\t", " ").replace("\n", " ")

    lines = ["\t".join(TSV_COLUMNS)]
    for r in plan["rows"]:
        lines.append("\t".join(cell(r.get(c)) for c in TSV_COLUMNS))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- apply


def _write_json(path: Path, payload: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _safe_rel(rel: str) -> bool:
    parts = rel.split("/")
    return len(parts) == 2 and all(p not in ("", ".", "..") for p in parts) and "\\" not in rel


def apply_plan(plan_path: Path, vault_override: Optional[Path], undo_log: Optional[Path]) -> dict:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    vault = vault_override or Path(plan["vault"])
    undo_path = undo_log or plan_path.with_suffix(".undo.json")
    undo: List[dict] = []
    results = {"renamed": 0, "refused": 0, "skipped": 0, "undo_log": str(undo_path), "refusals": []}

    def refuse(rel: str, why: str) -> None:
        results["refused"] += 1
        results["refusals"].append({"vault_rel": rel, "reason": why})

    for row in plan["rows"]:
        if row.get("action") != RENAME:
            results["skipped"] += 1
            continue
        rel, new_rel = row.get("vault_rel", ""), row.get("new_vault_rel", "")
        if not (_safe_rel(rel) and _safe_rel(new_rel)) or rel.split("/")[0] != new_rel.split("/")[0]:
            refuse(rel, "unsafe_path")
            continue
        src, dst = vault / rel, vault / new_rel
        if not src.is_file():
            refuse(rel, "source_missing")
            continue
        if os.path.lexists(dst):
            refuse(rel, "target_exists")
            continue
        try:
            current = file_hash(src)
        except (OSError, ValueError):
            refuse(rel, "source_unreadable")
            continue
        if not row.get("vault_sha256") or current != row["vault_sha256"]:
            refuse(rel, "hash_changed")
            continue
        os.rename(src, dst)
        undo.append({"from": new_rel, "to": rel, "sha256": current})
        results["renamed"] += 1
        # Persist after every rename so an interrupted run is still undoable.
        _write_json(undo_path, {"vault": str(vault), "plan": str(plan_path), "renames": undo})
    if not undo:
        _write_json(undo_path, {"vault": str(vault), "plan": str(plan_path), "renames": []})
    return results


# ----------------------------------------------------------------------------- CLI


def _default_drive_root() -> Optional[Path]:
    try:
        from scripts.drive_yt_summary.config import discover_yt_summary_root

        return discover_yt_summary_root()
    except Exception:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", help="vault root (default: $OUTPUT_MD_PATH)")
    ap.add_argument("--drive-root", help="Drive YT_summary folder (default: auto-discover)")
    ap.add_argument("--state", help="drive_yt_summary_sync_state.json (default: $WORK_PATH/index/...)")
    ap.add_argument("--date", default="", help="limit the vault scan to one YYYY_MM_DD folder")
    ap.add_argument("--out-json", help="write the plan JSON here (default: not written)")
    ap.add_argument("--out-tsv", help="write the plan TSV here (default: stdout)")
    ap.add_argument("--apply", metavar="PLAN.json", help="apply RENAME rows of a saved plan (vault only)")
    ap.add_argument("--undo-log", help="undo log path for --apply (default: PLAN.undo.json)")
    args = ap.parse_args(argv)

    if args.apply:
        res = apply_plan(
            Path(args.apply),
            Path(args.vault).expanduser() if args.vault else None,
            Path(args.undo_log) if args.undo_log else None,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 1 if res["refused"] else 0

    vault_s = args.vault or os.getenv("OUTPUT_MD_PATH", "")
    work = os.getenv("WORK_PATH", "")
    state_s = args.state or (str(Path(work) / "index" / "drive_yt_summary_sync_state.json") if work else "")
    drive = Path(args.drive_root).expanduser() if args.drive_root else _default_drive_root()
    if not vault_s or not state_s or drive is None:
        ap.error("need --vault, --state and --drive-root (or the env/auto-discovery for them)")
    vault, state_path = Path(vault_s).expanduser(), Path(state_s).expanduser()
    if not vault.is_dir() or not state_path.is_file():
        ap.error(f"vault or state not found: {vault} / {state_path}")

    plan = build_plan(vault, drive, state_path, args.date)
    if args.out_json:
        _write_json(Path(args.out_json), plan)
    tsv = plan_to_tsv(plan)
    if args.out_tsv:
        Path(args.out_tsv).write_text(tsv, encoding="utf-8")
    else:
        sys.stdout.write(tsv)
    print(f"dry-run plan: {json.dumps(plan['counts'], ensure_ascii=False)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
