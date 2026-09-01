# SUE-401 — P03 → Google Drive Desktop YT_summary sync

## Active transport (V1)

P03 finalized Obsidian Markdown → **local Google Drive Desktop mount** → `YT_summary/source/` + `manifest.yaml` → Google Drive Desktop cloud sync.

No Google Drive API upload, OAuth, or service-account credentials in the active path.

## Configuration

| Variable | Purpose |
|----------|---------|
| `OUTPUT_MD_PATH` | P03 summary source (Obsidian) |
| `P03_DRIVE_SYNC_ROOT` | Local `YT_summary` folder (auto-discovered if unset) |
| `P03_DRIVE_SYNC_ENABLED` | `0` disables sync (P03 summary unaffected) |

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

`{index}/drive_yt_summary_sync_state.json` — local idempotent sync state (not a workflow DB).
