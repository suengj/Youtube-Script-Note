# launchd reset (Bootstrap failed: 5)

For background (`78 EX_CONFIG`, log paths, troubleshooting), see [SCHEDULING.md](SCHEDULING.md) §2 and [LAUNCHD.md](LAUNCHD.md).

## Recommended: install script

```bash
cd $PROJECT_ROOT
export P03_LAUNCHD_SRC="${P03_LAUNCHD_SRC:-$PROJECT_ROOT/launchd}"
./scripts/install_launchd.sh
launchctl kickstart -k "gui/$(id -u)/com.user.p03-speech2text"
```

- Edit `launchd/com.user.p03-speech2text.plist.example` (or your copy) before install.
- Wrapper is deployed to `~/Library/Application Support/com.user.p03-speech2text/run-p03-speech2text.sh`.

## What each scheduled run does

Processes **channel crawl** (when `CHANNEL_CRAWL=true`) and URLs in **`input_df.csv`** in one batch.

## Python environment

- **launchctl commands** (`bootout`, `bootstrap`, `kickstart`): any shell environment.
- **Scheduled `main.py`**: the wrapper `exec`s the Python path you configure in `run-p03-speech2text.sh`.

## Force one run now

```bash
launchctl kickstart -k gui/$(id -u)/com.user.p03-speech2text
```

---

## Manual re-register (if install script fails)

```bash
P03=$PROJECT_ROOT
PLIST_SRC="${P03_LAUNCHD_SRC:-$P03/launchd}/com.user.p03-speech2text.plist"
WRAPPER_SRC="${P03_LAUNCHD_SRC:-$P03/launchd}/run-p03-speech2text.sh"
launchctl bootout gui/$(id -u)/com.user.p03-speech2text 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.user.p03-speech2text.plist
ln "$PLIST_SRC" ~/Library/LaunchAgents/com.user.p03-speech2text.plist
mkdir -p "$HOME/Library/Application Support/com.user.p03-speech2text" "$HOME/Library/Logs/p03-speech2text" "$P03/logs"
cp -f "$WRAPPER_SRC" "$HOME/Library/Application Support/com.user.p03-speech2text/run-p03-speech2text.sh"
chmod +x "$HOME/Library/Application Support/com.user.p03-speech2text/run-p03-speech2text.sh"
plutil -lint ~/Library/LaunchAgents/com.user.p03-speech2text.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.p03-speech2text.plist
launchctl list | grep p03-speech2text
```

Schedule times are defined in your plist's `StartCalendarInterval` entries.
