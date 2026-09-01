# Migration 2026-07-11: YTT_AUDIO → unified project root

## Summary

Hot runtime data moved from `~/YTT_AUDIO` into `$PROJECT_ROOT`.  
`BASE_PATH`, `WORK_PATH`, and `DATA_ROOT` now all point at the project tree.

## Source of truth (Jul 11)

| Asset | Canonical location |
|-------|-------------------|
| Hot CSV / JSONL | `p03_speech2text/data/` (rsync from `~/YTT_AUDIO/data/`) |
| `audio/`, `yt_subs/`, `tmp/`, `cache/`, `index/` | project root |
| Code, `output_new/`, `logs/` | project root (unchanged) |
| Obsidian MD output | `OUTPUT_MD_PATH` (iCloud vault) — unchanged |

Stale Jun 28 CSV at project root and iCloud copy were **not** imported; archived under `backup/legacy_root_csv_20260711/`.

## `.env` (local only, not in git)

```
BASE_PATH="$PROJECT_ROOT"
WORK_PATH="$PROJECT_ROOT"
DATA_ROOT="$PROJECT_ROOT/data"
```

## launchd

- Plist source: `~/Developer/PJT/launchd/com.user.p03-speech2text.plist`
- Install: `scripts/install_launchd.sh`
- `WORK_PATH` / `TMPDIR` / `XDG_CACHE_HOME` → project subdirs
- **`com.user.p03-data-mirror-icloud` retired** — bootout + remove plist

## Retired

- `~/YTT_AUDIO` as WORK_PATH (keep 1–2 weeks for rollback, then delete)
- `scripts/mirror_data_root_to_icloud.py` default off (`P03_DISABLE_ICLOUD_MIRROR=1`)

## Rollback (emergency)

1. Stop launchd: `launchctl bootout gui/$(id -u)/com.user.p03-speech2text`
2. Restore `.env` from `backup/pre_unify_20260711/.env.bak`
3. Point plist `WORK_PATH` back to `~/YTT_AUDIO` and re-run old install flow
4. Pre-migration CSV snapshot: `backup/pre_unify_20260711/`

## Post-migration checklist

- [x] `resolve_data_root()` → `.../data`
- [x] `launchctl list | grep p03-speech2text` shows loaded job
- [ ] One kickstart completes without Errno 11 storm
- [x] Jul 11 batch `output_new/` copied from legacy iCloud `BASE_PATH` → unified tree (64 files)
- [ ] Delete legacy tree: `~/Documents/Code/Python/PJT/p03_speech2text` (after user confirms)
- [ ] Optional: delete `~/YTT_AUDIO` after 1–2 week validation
