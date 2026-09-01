# Retired Drive API transport (SUE-401)

This directory quarantines the **abandoned Google Drive API upload path** for SUE-401.

## Why retired

- Service accounts can access and move folders in a shared personal My Drive folder but
  **cannot upload file bodies** (`403 storageQuotaExceeded`).
- Installed-app OAuth adds token lifecycle complexity without being required for this bridge.

## Active transport (SUE-401)

P03 finalized Markdown → **local Google Drive Desktop mount** → `YT_summary/source/` + `manifest.yaml`
→ Google Drive Desktop cloud sync.

Implementation: `scripts/drive_yt_summary/` (filesystem transport).

## Zero runtime weight

- Not imported by `main.py`, `sync_yt_summary_to_drive.py`, or launchd jobs.
- Not part of default pytest suite.
- No credential resolution at P03 startup.

Safe to delete this directory without changing production behavior.
