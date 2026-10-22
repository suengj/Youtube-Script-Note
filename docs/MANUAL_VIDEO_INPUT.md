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

After the batch, successful and Drive-verified ledger rows with `sheet_synced=false` are written back. The writer re-reads the tab, finds every matching row by normalized video ID from `URL`, sets `Download 여부` to the model that generated the summary, and sets `Source` to `own` only when that cell is blank. It writes only those exact cells with RAW values and reads them back. `sheet_synced` becomes true only after the readback matches; failures stay pending for a later run. Formula columns and all other cells are untouched.
