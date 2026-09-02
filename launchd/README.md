# launchd scheduling (macOS)

Example LaunchAgent files for periodic `main.py` runs. Copy and edit absolute paths before installing.

## Files

| File | Purpose |
|------|---------|
| `com.user.p03-speech2text.plist.example` | LaunchAgent schedule + environment |
| `run-p03-speech2text.sh.example` | Wrapper that `exec`s your Python interpreter |

## Install

1. Copy the example files and replace `/path/to/Youtube-Script-Note` and `/path/to/python` with your paths.
2. Set `StartCalendarInterval` in the plist to your desired schedule (e.g. every few hours).
3. Run from the repo root:

```bash
export P03_LAUNCHD_SRC=/path/to/edited/launchd
./scripts/install_launchd.sh
```

See [docs/LAUNCHD.md](../docs/LAUNCHD.md) and [docs/SCHEDULING.md](../docs/SCHEDULING.md) for operations and troubleshooting.
