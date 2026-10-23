# Canonical YT_summary Manifest Contract

## Authority and file identity

The production API writer updates the existing canonical `YT_summary/manifest.yaml` by Drive file ID. The ID is `P03_MANIFEST_FILE_ID` when set, otherwise `1NUIcUNlEyYwjHVngghwNlmh4VKiIk7oW`. It verifies the file name and `YT_summary` parent and never creates a second canonical manifest. Source hashes are SHA-256 over the exact Markdown bytes uploaded to Drive, including any UTF-8 BOM. `file` remains the reader-facing source reference; `drive_file_id` is also recorded when available.

The API sink is the only production writer. `scripts/drive_yt_summary/sync.py` writes `manifest.yaml` only when `P03_MANIFEST_WRITER=desktop` is explicit and `P03_DRIVE_BACKEND` is not `api`. Without that explicit setting it refuses before copying files. A manifest with inventory or lifecycle fields is never rebuilt: an explicitly authorized desktop run merges entries and preserves unknown top-level fields, unknown items, and lifecycle fields.

## Per-source lifecycle

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
- A changed hash increments `revision` and sets `content_revision_at` to the actual update time. Existing `finalized_at` and all unrelated fields remain intact.
- A verified Drive dedupe hit with no manifest item creates a recovered item with `finalized_at: null` and actual `recovered_at`.
- A recovered item with a different hash increments its revision, sets `recovered_at`, and removes `content_revision_at`: recovery time does not establish when the content changed.
- Legacy entries without lifecycle timestamps stay without them. Video upload dates and Drive modified times are never used to infer finalization or revision time.
- If an existing legacy item lacks a hash, the observed hash may be recorded without incrementing the revision or inventing a lifecycle timestamp.

Identity matches consider Drive file ID, file reference, and video ID. Multiple conflicting matches or a conflicting video identity fail closed rather than creating a duplicate source entry.

## Inventory completion evidence

The canonical manifest contains the run inventory details, but it **always** stores `manifest_readback_verified: false` and `inventory_complete: false`. A manifest cannot establish that its own bytes were read back. A completed run publishes its verified completion in one sibling Drive file named `manifest.inventory.json`:

```json
{
  "schema_version": 1,
  "manifest_file_id": "1NUIcUNlEyYwjHVngghwNlmh4VKiIk7oW",
  "manifest_sha256": "<sha256 of exact current manifest.yaml bytes>",
  "inventory": {
    "inventory_revision": 1,
    "producer_run_id": "20261023010203-a1b2c3d4",
    "coverage_started_at": "2026-10-23T01:02:03Z",
    "coverage_completed_at": "2026-10-23T01:07:00Z",
    "cutoff": {"mode": "channel_cursor"},
    "source_counts": {"succeeded": 1, "failed": 0, "unresolved": 0, "scope_count": 1, "terminal_skipped": 0},
    "discovery_complete": true,
    "manifest_readback_verified": true,
    "all_successes_present": true,
    "batch_completed": true,
    "inventory_complete": true
  }
}
```

The write sequence is:

1. Write the provisional or completed run details into `manifest.yaml` with both completion flags false.
2. Re-read Drive `headRevisionId` and `md5Checksum` immediately before the update. If either changed since the merge read, restart from the latest document. After update, download the manifest and require the exact payload SHA-256 and Drive metadata MD5 to match.
3. Only after that manifest readback succeeds, create or update the one `manifest.inventory.json` file in the same parent. On first use, the writer searches the exact name under the parent; it refuses multiple matches. Later writes update the discovered fixed file ID in place. A configured `P03_INVENTORY_FILE_ID` pins that identity explicitly.
4. Download the evidence file and require its SHA-256 to match the uploaded bytes, its MD5 to match Drive metadata, and its parsed JSON to equal the intended evidence. The API call returns a complete receipt only after this check.

A consumer trusts completion only after it reads both current files, verifies the evidence download against Drive metadata MD5, and checks `manifest_sha256 == SHA256(exact current manifest.yaml bytes)`. It also requires `inventory_complete`, `manifest_readback_verified`, `batch_completed`, `discovery_complete`, `all_successes_present`, and zero `failed` and `unresolved` counts. A later manifest mutation makes old evidence fail the SHA-256 match. The artifact's completion fields alone are never sufficient. If the producer crashes before publishing evidence there is no new completion; if it crashes during evidence publication, consumers still fail closed unless their own reads verify both artifacts and the exact manifest match.

## Coverage scope and cursors

`inventory.source_counts` reports `succeeded`, `failed`, `unresolved`, `scope_count`, and `terminal_skipped`. The scope includes pending channel queue rows and selected manual sources before `VIDEO_ID_FILTER` or `MAX_VIDEOS` can defer work. Active-claim skips, max-video deferrals, and retry-throttled/deferred rows without a terminal result remain unresolved. Retry-exhausted queue failures count as failed. Success and verified terminal exclusions (such as shorts, unavailable sources, policy skips, or oversized sources) are recorded by source ID; successful manifest sources must all be present with a valid SHA-256. Invalid discovery entries add unresolved count.

`inventory_complete` in the detached evidence can be true only when the batch returned, discovery completed, `failed == 0`, `unresolved == 0`, every successful source is present, and both manifest and evidence bytes passed readback. An omitted selected source therefore cannot disappear from the counts and create a zero-work completion.

The channel discovery cursor advances only after that channel's pagination completes and every discovered entry has a resolvable publication timestamp and video ID. Incomplete pagination or malformed entries preserve the prior `last_discovered_published_at` and exclude that channel from `last_processed_published_at` advancement in the same run, including when `last_processed_published_at` is the fallback discovery cursor. Accepted items can still be queued and rediscovered safely on the next run.

The queue loader casts text and timestamp columns to mutable object dtype. Blank CSV timestamp columns may otherwise load as `float64` under pandas 3.x and reject later string assignments in `SharedStateWriter.apply()`.

## Write and failure behavior

The API writer uses a host-wide advisory lock for its canonical manifest ID. Each attempt reads and merges the latest YAML, re-reads `headRevisionId` and `md5Checksum` immediately before update, then verifies the manifest bytes after update. It checks that all item identities from the pre-read document remain present; unexpected revisions or changed readbacks cause a bounded re-merge/retry. Missing manifest, malformed YAML, exhausted retries, upload errors, or failed readback raise `DriveUploadError`. In that case `process_single_video` returns a retryable non-success result and the processed-video success registry is not updated. A later verified dedupe retry repairs a missing source entry without rerunning summary generation.

Drive has no compare-and-swap update for this file. The lock protects cooperating API writers, and the production desktop writer is refused, but an unauthorized external writer can still race in the narrow interval after the final metadata check. Revision jumps and post-write content checks detect many such races. If a racing write is overwritten before its content is observed, Drive's current API path cannot reconstruct that unseen payload from the manifest alone; the single-writer rule is what prevents this case in normal production.

The desktop path operates on a Drive Desktop filesystem mount, not `files.update`, so Drive `headRevisionId` is unavailable there. It re-reads the local manifest SHA-256/MD5 immediately before atomic replacement, checks the post-write bytes and every pre-read item identity, and retries a bounded merge when a local clobber is observed. A different host can still sync after the last local check; the desktop writer is not a production authority and cannot provide remote compare-and-swap protection.

## Registry consumer compatibility

The current read-only consumer is `../registry/production-v2.yaml` (this checkout does not contain the nested `../registry/registry/production-v2.yaml` path named in the brief). It selects sources by manifest file/stable source ID plus content hash and reads `finalized_at` or `content_revision_at`; those source fields remain compatible. It already requires fresh complete producer evidence, preserves `FRESHNESS_UNVERIFIED`, caps runs at 100, and keeps checkpoint generation, processed state, and pending state consumer-owned.

The current Registry text does not explicitly map producer inventory fields to checkpoint fields or describe the detached `manifest.inventory.json` checksum rule. Do not edit `../registry`. Proposed exact patch for its `jobs.p03-content.youtube-digest.daily_updates` block:

```diff
@@ -310,1 +310,14 @@
         freshness: require_current_run_read_of_canonical_manifest_and_producer_evidence_of_complete_slot_inventory_coverage_never_trust_manifest_generation_date_alone
+        producer_inventory_evidence:
+          canonical_manifest: YT_summary/manifest.yaml
+          completion_file: YT_summary/manifest.inventory.json
+          completion_file_selection: exact_name_under_same_parent_exactly_one_file_or_FRESHNESS_UNVERIFIED
+          verify_completion_file_download: Drive_metadata_md5_matches_exact_downloaded_bytes
+          manifest_sha256_rule: completion_file.manifest_sha256_equals_SHA256_of_exact_current_canonical_manifest_bytes
+          require_inventory_fields: [inventory_complete, manifest_readback_verified, batch_completed, discovery_complete, all_successes_present]
+          require_counts: {failed: 0, unresolved: 0}
+          require_coverage_completed_at: non_null_and_no_later_than_frozen_run_start
+          checkpoint_mapping:
+            daily_updates.coverage_end: inventory.coverage_completed_at
+            daily_updates.inventory_revision: inventory.inventory_revision
+            daily_updates.producer_coverage_evidence: [manifest_file_id, completion_file_id, manifest_sha256, producer_run_id, inventory_revision, coverage_started_at, coverage_completed_at, cutoff, source_counts, discovery_complete]
         stale_or_unknown_inventory: stop_daily_selection_report_FRESHNESS_UNVERIFIED_do_not_claim_zero_new_or_advance_checkpoint_or_fill_from_pending
@@ -318,1 +331,1 @@
-        empty_window: only_verified_fresh_complete_inventory_may_record_zero_new_and_advance_daily_coverage_with_checkpoint_readback_no_digest_or_old_source_substitution
+        empty_window: only_after_the_detached_producer_inventory_evidence_passes_all_checksum_and_inventory_complete_rules_may_record_zero_new_and_advance_daily_coverage_with_checkpoint_readback_no_digest_or_old_source_substitution
```

This mapping supplies the producer evidence required for the existing checkpoint fields; it does not change the consumer's source identity, 100-source cap, pending rules, or historical baseline.

## Verification

Synthetic tests use fake Drive and local filesystem fixtures; they do not call Google APIs or LLMs. Regressions cover completion evidence readback, source-scope reconciliation, cursor retention after partial pagination, pandas queue loading and mutation, recovered revisions, and desktop manifest authority/merge retries.

```sh
../venv-p3/bin/python -m pytest -q tests
/opt/homebrew/Caskroom/miniforge/base/envs/ai/bin/python -m pytest -q tests
```
