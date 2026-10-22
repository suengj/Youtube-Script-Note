# Individual YouTube video input

## Owner steps

In the **YT Link** tab, paste each individual YouTube video URL in column B under the `URL` header. Optionally set `Source` to `own`; leave `Download 여부` empty. The importer reads the sheet and appends unseen video IDs to `data/input_df.csv`; the next batch run picks them up.

Configure the service account key path in `P03_GOOGLE_SA_JSON` (deployed path: `/Users/suengj/Developer/api_key/p03_google_sa.json`). The key needs access to the spreadsheet. The integration requests only the `https://www.googleapis.com/auth/spreadsheets` scope. The spreadsheet defaults to `1iRtWdbbJGM8EY1K0jDXkt36IImQM-BmhDoufsFOjRKI` and tab `YT Link`; override with `P03_GOOGLE_SHEET_ID` / `P03_GOOGLE_SHEET_TAB` or importer `--spreadsheet-id` / `--tab`.

Run from the repository root:

```bash
python scripts/import_manual_urls_from_sheet.py --dry-run
python scripts/import_manual_urls_from_sheet.py
```

There is no CSV-export import mode. Before reading or writing values, the script checks that the tab exists and row 1 contains `URL`, `Source`, and `Download 여부`. An error aborts sheet I/O with a log message; local ledger state remains usable. The importer normalizes URL forms and deduplicates by video ID. It preserves existing `input_df.csv` rows and appends only new video IDs, retaining date/title/source where those columns exist.

## Processing, retries, and sheet status

The manual queue is deduplicated by normalized video ID and merged with channel candidates, which retain channel metadata and policy. Failures, unsupported URLs, and policy skips remain in the local `data/manual_input_status.csv` ledger and are never written to the sheet. The retry limit is `CRAWL_QUEUE_MAX_RETRIES`.

A manual ledger entry becomes `success` only after final Markdown is published and verified by the configured Drive publisher. If a publish falls back to local staging, the ledger stays `retryable_failed` with `pending_drive`; the next run retries that staged Markdown publish before invoking the LLM again. Channel-crawl staging behavior is unchanged.

Manual items follow the same existing policies as channel items by default. When YouTube duration metadata is available, the Shorts duration filter applies; a filtered item is recorded as `skipped_policy` with its threshold reason in the ledger and is never written to the Sheet. If the video's channel is registered in `channel_df.csv` and its `auto_sub_only` flag is enabled, the downloader proceeds when accepted uploader or automatic captions exist and skips only when neither exists. Missing policy metadata does not invent a policy decision. Manual policy lookup reads the same `DATA_ROOT/channel_df.csv` registry used by channel discovery.

After the batch, successful and Drive-verified ledger rows with `sheet_synced=false` are written back. The writer locates matching rows by normalized video ID, then immediately snapshots each target row's `URL`, `Source`, and `Download 여부` with one `values.batchGet`. It verifies the URL identity, writes only `Source` and `Download 여부`, and reads those three cells back together. `sheet_synced` becomes true only when the URL still identifies the target and both written values match. A detected movement is logged and recorded as `needs_manual_check`; the writer never restores target values onto a different row. Google Sheets has no compare-and-set for these cell writes, so a row can still move in the narrow interval between the identity snapshot and `batchUpdate`. Readback catches this case, but if the new row content no longer matches the snapshot URL, the writer leaves the cells untouched and requires manual review; concurrent row edits remain a residual race. Formula columns and all other cells are untouched.
