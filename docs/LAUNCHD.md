# launchd (macOS scheduling)

Example plist and wrapper templates ship in **`launchd/`** at the repo root.  
Install with **`scripts/install_launchd.sh`** (copies wrapper to Application Support, links plist into LaunchAgents, bootstraps).

## Summary

| Item | Value |
|------|-------|
| Label | `com.user.p03-speech2text` |
| Schedule | `StartCalendarInterval` in your plist (see `launchd/*.example`) |
| `WORK_PATH` | Project root (`$PROJECT_ROOT`) |
| `TMPDIR` / `XDG_CACHE_HOME` | `{WORK_PATH}/tmp`, `{WORK_PATH}/cache` |
| Runner | `~/Library/Application Support/com.user.p03-speech2text/run-p03-speech2text.sh` |
| App logs | `logs/stt_YYYYMMDD.log` |
| launchd I/O | `~/Library/Logs/p03-speech2text/launchd_*.log` |

## Install / re-register

```bash
cd $PROJECT_ROOT
# Optional: point at your edited launchd directory
export P03_LAUNCHD_SRC="$PROJECT_ROOT/launchd"
chmod +x scripts/install_launchd.sh
./scripts/install_launchd.sh
```

## Manual one-shot run

```bash
launchctl kickstart -k "gui/$(id -u)/com.user.p03-speech2text"
```

If you see `Could not find service`, run `./scripts/install_launchd.sh` first.

## Troubleshooting: batch `download_failed`

If `logs/stt_YYYYMMDD.log` shows repeated **`[Errno 32] Broken pipe`**, suspect **yt-dlp → ffmpeg pipe/network** rather than a single bad video. Try: update yt-dlp, verify ffmpeg on `PATH`, check disk space under `WORK_PATH`, review VPN/proxy, and reproduce with `yt-dlp -v 'URL'`.

## `78 EX_CONFIG` / plist present but job won't start

Exit code **78** often means launchd failed before `main.py` ran. Confirm the Application Support wrapper path, log directory under `~/Library/Logs/p03-speech2text/`, and that plist paths match your machine. After edits, re-run `./scripts/install_launchd.sh`.

## After plist or wrapper changes

`./scripts/install_launchd.sh` (bootout → link plist → bootstrap). If LaunchAgents plist is a hard link, some editors break the link on save — re-run the install script.

## iCloud DATA mirror (retired)

`com.user.p03-data-mirror-icloud` is **disabled**. `scripts/mirror_data_root_to_icloud.py` defaults off (`P03_DISABLE_ICLOUD_MIRROR=1`).

Background: [MIGRATION_20260711.md](MIGRATION_20260711.md)
