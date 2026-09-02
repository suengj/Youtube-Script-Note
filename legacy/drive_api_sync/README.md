# Retired Drive API transport

This directory quarantines the **abandoned Google Drive API upload path** for YT_summary sync.

## Active transport

Filesystem copy via **Google Drive Desktop** — see [docs/DRIVE_SYNC.md](../../docs/DRIVE_SYNC.md).

## Why this code is quarantined

Service-account API uploads hit `403 storageQuotaExceeded` for personal My Drive folders. The active path uses Drive Desktop sync instead.
