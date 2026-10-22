# Individual YouTube video input

## Owner steps

In the **YT Link** tab, paste each individual YouTube video URL in column B under the `URL` header. Optionally set `Source` to `own`; leave `Download 여부` empty. A batch automatically imports eligible video IDs into `<DATA_ROOT>/input_df.csv` when `P03_SHEET_IMPORT_ENABLED=1`; the default is `0` (off). Set `P03_SHEET_IMPORT_SINCE=YYYY-MM-DD` to limit batch import to rows whose `Updated/Published Time/` date is on or after that date. If it is missing or invalid, the batch logs a warning and skips sheet import while continuing. `P03_SHEET_IMPORT_MAX` limits each run to the newest eligible rows and defaults to `5`. The normal batch queue then picks them up in that run. The import runs under the existing batch run lock. If the service-account key is missing or sheet import fails, the batch logs a warning and continues with its existing queues.

Set `P03_SHEET_IMPORT_ENABLED=1` and configure the service-account key path in `P03_GOOGLE_SA_JSON` (deployed path: `/Users/suengj/Developer/api_key/p03_google_sa.json`). The key needs access to the spreadsheet. The integration requests only the `https://www.googleapis.com/auth/spreadsheets` scope. The spreadsheet defaults to `1iRtWdbbJGM8EY1K0jDXkt36IImQM-BmhDoufsFOjRKI` and tab `YT Link`; override with `P03_GOOGLE_SHEET_ID` / `P03_GOOGLE_SHEET_TAB` or importer `--spreadsheet-id` / `--tab`.

Run from the repository root:

```bash
python scripts/import_manual_urls_from_sheet.py --since 2026-10-01 --dry-run
python scripts/import_manual_urls_from_sheet.py --since 2026-10-01
# Explicitly allow reading all dates (still subject to P03_SHEET_IMPORT_MAX):
python scripts/import_manual_urls_from_sheet.py --all --dry-run
```

For Linux systemd deployment, set `P03_SHEET_IMPORT_ENABLED=1` and `P03_GOOGLE_SA_JSON=/path/to/service-account.json` in the existing `%h/.config/p03/p03.env` or `p03-locators.env` `EnvironmentFile`; keep the key outside the repository and readable by the service user. Add the same variables to the environment used by other deployment methods. There is no new scheduler.

The CLI requires `--since YYYY-MM-DD` unless `--all` is explicitly supplied. With a cutoff, rows with an empty or unparseable `Updated/Published Time/` cell are skipped. There is no CSV-export import mode. Before reading or writing values, the script checks that the tab exists and row 1 contains `URL`, `Source`, and `Download 여부`. An error aborts sheet I/O with a log message; local ledger state remains usable. The importer normalizes URL forms and deduplicates by video ID. Rows with a non-empty `Download 여부`, any matching video ID in `output_df_new.csv`, the manual ledger, or `input_df.csv` are counted as already processed. It preserves existing `input_df.csv` rows and appends only new video IDs, retaining date/title/source where those columns exist.

## Processing, retries, and sheet status

The manual queue is deduplicated by normalized video ID and merged with channel candidates, which retain channel metadata and policy. Failures, unsupported URLs, and policy skips remain in the local `data/manual_input_status.csv` ledger and are never written to the sheet. The retry limit is `CRAWL_QUEUE_MAX_RETRIES`.

A manual ledger entry becomes `success` only after final Markdown is published and verified by the configured Drive publisher. If a publish falls back to local staging, the ledger stays `retryable_failed` with `pending_drive`; the next run reuses the staged Markdown only when its SHA-256 matches the ledger, keeps it until exact destination readback succeeds, and otherwise returns the item to normal pipeline reprocessing. Channel-crawl staging behavior is unchanged.

Manual items follow the same existing policies as channel items by default. When YouTube duration metadata is available, the Shorts duration filter applies; a filtered item is recorded as `skipped_policy` with its threshold reason in the ledger and is never written to the Sheet. If the video's channel is registered in `channel_df.csv` and its `auto_sub_only` flag is enabled, the downloader proceeds when accepted uploader or automatic captions exist and skips only when neither exists. Missing policy metadata does not invent a policy decision. Manual policy lookup reads the same `DATA_ROOT/channel_df.csv` registry used by channel discovery.

After the batch, successful and Drive-verified ledger rows with `sheet_synced=false` and no owner hold are written back. For each item, the writer snapshots every row's row number, exact `URL` text, normalized video ID, `Source`, and `Download 여부`. It writes every row matching the target video ID, setting `Source` to `own` only where that row's source was blank. It then reads the rows again. When row number, URL text, and video ID are unchanged, changed non-target cells are repaired by row. If that sequence changed, a non-target row is repaired by video ID only when that ID appears exactly once in both snapshots. Duplicate or missing IDs are left untouched when their row identity is ambiguous. Any detected anomaly records `sheet_synced=false`, `needs_manual_check=true`, a ledger reason, and an error log even if repair succeeded. An owner must clear `needs_manual_check` before a later run can write that item. A clean pass sets `sheet_synced=true` only when the row sequence is identical, every target row matches its expected values, and every non-target row is unchanged. Google Sheets has no compare-and-set, so a concurrent edit after final readback remains a residual race. Formula columns and all other cells are untouched.

Manual staged Markdown is published only when its SHA-256 matches the ledger. A missing or mismatched stage is moved to `WORK_PATH/quarantine/manual_staging/` and excluded from the batch flush scan. Regeneration uses `regen_attempts` independently of the ordinary pipeline retry count, with a cap of two; after the cap the ledger reason is `regen cap reached`.
