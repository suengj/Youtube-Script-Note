# Migration 2026-07-11: unified project root layout

## Summary

Runtime data (CSV queues, cache, audio, temp files) lives under **`$PROJECT_ROOT`** instead of a separate legacy work directory.  
`BASE_PATH`, `WORK_PATH`, and `DATA_ROOT` should all resolve to your project tree (see `.env.example`).

## Canonical layout

| Asset | Location |
|-------|----------|
| Hot CSV / JSONL | `$PROJECT_ROOT/data/` |
| `audio/`, `yt_subs/`, `tmp/`, `cache/`, `index/` | project root |
| Code, `output_new/`, `logs/` | project root |
| Markdown output | `OUTPUT_MD_PATH` (your vault or output folder) |

## `.env` (local only)

```
BASE_PATH="$PROJECT_ROOT"
WORK_PATH="$PROJECT_ROOT"
DATA_ROOT="$PROJECT_ROOT/data"
```

## launchd

- Plist template: `launchd/com.user.p03-speech2text.plist.example`
- Install: `scripts/install_launchd.sh` with `P03_LAUNCHD_SRC` pointing at your edited `launchd/` directory
- Set `WORK_PATH`, `TMPDIR`, and `XDG_CACHE_HOME` to project subdirectories in the plist
- **iCloud data-mirror LaunchAgent retired** — remove `com.user.p03-data-mirror-icloud` if present

## Retired

- Separate legacy `WORK_PATH` trees (migrate data into `data/` then decommission)
- `scripts/mirror_data_root_to_icloud.py` default off (`P03_DISABLE_ICLOUD_MIRROR=1`)

## Rollback (emergency)

1. Stop launchd: `launchctl bootout gui/$(id -u)/com.user.p03-speech2text`
2. Restore `.env` from your backup
3. Point plist `WORK_PATH` at the previous layout and re-install
4. Restore CSV snapshots from your backup if needed

## Post-migration checklist

- [ ] `resolve_data_root()` → `.../data`
- [ ] `launchctl list | grep p03-speech2text` shows loaded job
- [ ] One `kickstart` completes without I/O errors
- [ ] Decommission old work directories after validation
