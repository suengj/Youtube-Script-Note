# Mac Obsidian receiver

The Mac receiver pulls summaries from Drive folder `YT_summary/source` into the existing `OUTPUT_MD_PATH` vault. It reads the selected channel rows from `channel_df.csv` (`obsidian=recording`), validates Drive `p03_video_id` and `p03_kind=summary`, and uses the existing filename channel prefix when Drive has no `p03_channel_id` property. It processes at most `P03_OBSIDIAN_MAX` items per run (default 50).

The receiver writes `index/p03_obsidian_receive_state.json` beneath `WORK_PATH` (or `DATA_ROOT`) unless `P03_STATE_DIR` selects another state directory. Each ledger entry records Drive ID and MD5, the vault basename, and the SHA-256 written by the receiver. An unchanged Drive item is skipped. A Drive revision replaces a receiver-owned note only while its local SHA-256 still matches the ledger. A user-edited note is left untouched and the incoming version is written beside it as `.update-candidate.md`. Existing files without a ledger entry are treated as user-owned. The receiver never deletes vault notes.

Drive OAuth is loaded at runtime from the existing user token named by `P03_DRIVE_TOKEN_FILE`; the token value is never printed. The owner-managed env file is located by `P03_RECEIVE_ENV_FILE` in the LaunchAgent and should define `P03_DRIVE_TOKEN_FILE`, `OUTPUT_MD_PATH`, and optionally `P03_CHANNEL_CSV`, `P03_STATE_DIR`, `P03_OBSIDIAN_MAX`, `DATA_ROOT`, and `WORK_PATH`. Keep the env file and token outside the repository.

## Owner install

From the repository root on the Mac, substitute the actual checkout and username in these commands. The owner creates `~/.config/p03/obsidian-receive.env` with the variables above and the existing token file path; it must not contain the token JSON itself.

```sh
mkdir -p "$HOME/Library/Application Support/com.user.p03-obsidian-receive" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/p03-obsidian-receive" "$HOME/.config/p03"
cp launchd/run-p03-obsidian-receive.sh.example "$HOME/Library/Application Support/com.user.p03-obsidian-receive/run-p03-obsidian-receive.sh"
cp launchd/com.user.p03-obsidian-receive.plist.example "$HOME/Library/LaunchAgents/com.user.p03-obsidian-receive.plist"
chmod 700 "$HOME/Library/Application Support/com.user.p03-obsidian-receive/run-p03-obsidian-receive.sh"
chmod 600 "$HOME/.config/p03/obsidian-receive.env"
```

Edit the copied plist placeholders to the actual username and checkout path. Then load it:

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.user.p03-obsidian-receive.plist"
launchctl kickstart -k "gui/$(id -u)/com.user.p03-obsidian-receive"
```

## Owner uninstall

```sh
launchctl bootout "gui/$(id -u)/com.user.p03-obsidian-receive"
rm "$HOME/Library/LaunchAgents/com.user.p03-obsidian-receive.plist"
rm -rf "$HOME/Library/Application Support/com.user.p03-obsidian-receive"
```

The uninstall commands leave the vault, receive ledger, owner env file, OAuth token, and logs in place.
