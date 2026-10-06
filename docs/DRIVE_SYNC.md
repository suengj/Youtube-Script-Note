# Google Drive Desktop — YT_summary sync

## Active transport (V1)

Finalized Markdown summaries are copied to a **local Google Drive Desktop mount** under `YT_summary/source/` with an idempotent `manifest.yaml`. Google Drive Desktop handles cloud sync.

No Google Drive API upload, OAuth, or service-account credentials in the active path.

## Storage / mirror split (SUE-1298)

Per video, `scripts/drive_yt_summary/publish.py` runs after the final Markdown is generated (content unchanged):

1. **Staging** (`WORK_PATH/output_md_staging/`) - work/retry cache only. Deleted only after the Drive write is verified (local readback hash). Leftovers are retried by `flush_staging` at batch end.
2. **Drive Desktop** (`P03_DRIVE_SYNC_ROOT/source/`) - canonical, all channels. Same hash = no-op; changed content = same file, `revision` + 1 in state/manifest (with `video_id`, `hash`).
3. **Obsidian mirror** (`OUTPUT_MD_PATH`) - with `P03_SELECTIVE_MIRROR` off (default) every channel is mirrored to the vault and catalogued as before. With it on, only channels where `video_config["obsidian_mirror"]` is true are mirrored. The flag is flipped by SUE-1299 after proof. Whenever the Drive write is not ok, the note is mirrored regardless, so an outage never leaves it only in staging.

Drive and mirror failures are isolated. `P03_DRIVE_SYNC_ENABLED=0` keeps the legacy behaviour (everything to `OUTPUT_MD_PATH`). Cloud readback is SUE-1299; Linux direct API is SUE-1314. One writer per host (Desktop path active on Mac).

## Configuration

| Variable | Purpose |
|----------|---------|
| `OUTPUT_MD_PATH` | Pipeline Markdown output directory |
| `P03_DRIVE_SYNC_ROOT` | Local `YT_summary` folder (auto-discovered if unset) |
| `P03_SELECTIVE_MIRROR` | `1` mirrors only obsidian-marked channels to the vault (default off: mirror all) |
| `P03_DRIVE_SYNC_ENABLED` | `0` disables sync (pipeline output unaffected) |

## CLI

```bash
python scripts/sync_yt_summary_to_drive.py --dry-run
python scripts/sync_yt_summary_to_drive.py --limit 3
python scripts/sync_yt_summary_to_drive.py --backfill-date 2026-08-31
python scripts/sync_yt_summary_to_drive.py --migrate-legacy-only
```

## Retired API path

See `legacy/drive_api_sync/README.md` — quarantined; zero runtime weight.

### Why API was retired

Service accounts can access shared personal My Drive folders but cannot upload file bodies (`403 storageQuotaExceeded`). Drive Desktop filesystem sync avoids API quota and OAuth lifecycle.

## State

`{index}/drive_yt_summary_sync_state.json` — local idempotent sync state (not a workflow database).
