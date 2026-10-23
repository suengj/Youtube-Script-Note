# Canonical YT_summary Manifest Contract

## Authority and file identity

The API producer updates the existing canonical `YT_summary/manifest.yaml` by Drive file ID. The ID is `P03_MANIFEST_FILE_ID` when set, otherwise `1NUIcUNlEyYwjHVngghwNlmh4VKiIk7oW`. The writer verifies the file name and `YT_summary` parent, updates that ID in place, and never creates a replacement manifest.

The Linux production path is `DriveApiSink`; the desktop sync builder remains a separate path. Source hashes are SHA-256 over the exact Markdown bytes uploaded to Drive, including any UTF-8 BOM. `file` remains the reader-facing source reference. `drive_file_id` is also recorded for stable identity when available.

## Per-source lifecycle

An item retains all existing fields. The API writer changes only the matched item and preserves unrelated items and top-level manifest fields.

```yaml
items:
  - file: Example_abcdefghijk.md
    title: Example
    hash: <sha256 of exact Drive Markdown bytes>
    revision: 1
    video_id: abcdefghijk
    drive_file_id: <Drive source file ID>
    finalized_at: 2026-10-23T01:02:03Z
```

- A newly finalized source starts at `revision: 1` with its actual `finalized_at`.
- The same source file and hash is an idempotent no-op: no manifest write and no metadata backfill.
- A changed hash increments `revision` and sets `content_revision_at` to the actual update time. Existing `finalized_at` and all other legacy fields remain intact.
- A verified Drive dedupe hit with no manifest item creates a recovered item with `finalized_at: null` and actual `recovered_at`. Recovery time is never represented as finalization time.
- Legacy entries without lifecycle timestamps stay without them. Video upload dates and Drive modified times are not used to infer source finalization or revision time.
- If an existing legacy item lacks a hash, the observed hash may be recorded without incrementing the revision or inventing a lifecycle timestamp.

Identity matches consider Drive file ID, file reference, and video ID. Multiple conflicting matches or conflicting video identity fail closed rather than creating a duplicate source entry.

## Write and failure behavior

All local writers use a host-wide advisory file lock for the manifest ID. Each write reads and merges the latest YAML, then re-reads `headRevisionId` and `md5Checksum` immediately before updating. A changed head/checksum restarts the merge from the latest file, up to five attempts. The batch run lock remains in effect for normal scheduled runs.

Writes use `files.update` with the fixed manifest ID. After each update, the writer downloads the bytes and requires SHA-256 to match the exact serialized payload. Metadata MD5 is checked against the downloaded bytes too. Missing manifest, malformed YAML, exhausted conflict retries, upload errors, or failed readback raise `DriveUploadError`. In that case `process_single_video` returns a retryable non-success result; the processed-video success registry is not updated. A later verified dedupe retry repairs a missing entry without rerunning summary generation.

## Batch inventory coverage

The top-level `inventory` block describes one producer run:

```yaml
inventory:
  inventory_revision: 1
  producer_run_id: 20261023010203-a1b2c3d4
  coverage_started_at: 2026-10-23T01:02:03Z
  coverage_completed_at: 2026-10-23T01:07:00Z
  cutoff: {mode: channel_cursor, channels: {UCexample: {exclusive_after: '...'}}}
  source_counts: {succeeded: 1, failed: 0, unresolved: 0}
  discovery_complete: true
  manifest_readback_verified: true
  all_successes_present: true
  batch_completed: true
  inventory_complete: true
```

The writer first records an incomplete run with a null completion time. A normal batch return writes the final record. A hard interruption leaves the provisional record incomplete with `coverage_completed_at: null`; a returned batch with source or discovery failures records its completion time but keeps `inventory_complete: false`. `inventory_revision` increments once for each new run and remains the same when that run is finalized.

`cutoff` records the channel cursor/backfill boundaries used by discovery and the manual selection boundary when applicable. `source_counts` reports selected-scope successes, failures, and unresolved sources; malformed discovery records are counted as unresolved. `discovery_complete` is true only when every configured channel discovery call completed. The writer checks that every success ID has a source item with a valid hash before setting `all_successes_present`.

`inventory_complete` is true only when the batch completed, discovery completed, failed and unresolved counts are zero, every success is present, and the final manifest write passed SHA-256 readback. Any incomplete condition leaves the inventory false. Failure to write or verify the inventory is logged and cannot establish fresh coverage.

## Registry consumer compatibility

The read-only `../registry/production-v2.yaml` consumer selects by `file`/stable source ID plus `hash`, and reads `finalized_at` or `content_revision_at` for the daily window. The manifest provides those fields directly. Missing legacy timestamps remain unresolved as required. The consumer's 100-source limit, checkpoint `generation`/processed/pending state, `FRESHNESS_UNVERIFIED` rule, and zero-new rule remain consumer-owned and unchanged. Its checkpoint comparison can derive new and revised sources from item identity plus hash; a separate run list of source IDs is not required.

No registry mapping change is required or proposed. The Registry checkout remains read only.

## Verification

Synthetic Drive tests cover source creation, revisions, idempotency, legacy preservation, recovered dedupe, write/readback failures, inventory completion, conflict re-merge, concurrent writers, and the no-create rule. They use a fake Drive service and do not call Google APIs or LLMs.

Run the focused tests and complete suite with both required interpreters:

```sh
/opt/homebrew/Caskroom/miniforge/base/envs/ai/bin/python -m pytest -q
../venv-p3/bin/python -m pytest -q
```
