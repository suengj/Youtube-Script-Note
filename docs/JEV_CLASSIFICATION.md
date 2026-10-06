# JEV source classification (classification-index.json)

## Ownership

**P03 (suengj/Youtube-Script-Note) owns `YT_summary/classification-index.json` and is its only writer** (SUE-1327).
The canonical contract is `schemas/classification-index.schema.json` (schema_version 2). The earlier
intelligence-library `source_classify` implementation (SUE-1296/1301/1302) is historical evidence only and
is retired in SUE-1328; it must not write this file (it cannot read schema_version 2 safely). Other
systems (AES, intelligence-library, social-publisher) may read the index as derived input; they never write it.
`manifest.yaml` remains the source-lifecycle SSOT; the classifier never modifies source Markdown or `manifest.yaml`.

| Item | Value |
|------|-------|
| Index | `<P03_DRIVE_SYNC_ROOT>/classification-index.json` (Drive id `1KESOin7YU1QR0KSfoKIxhbkytTvQ1t2x`) |
| Identity / key | YouTube `video_id` from front matter `source_url` (or `vid`); Drive file id / name are `lineage` only |
| Provider | JEV / TypeSafe System One, `POST /v1/systemone`, model pin `jev-1.13.0` |
| Rubric | `scripts/jev_classify/jev.py`; `prompt_version = jev-rubric-<sha256(rubric+model)[:12]>` (currently `jev-rubric-b03a101ddb34`) |
| Taxonomy | `scripts/jev_classify/taxonomy.py`, `1.0.0` (mirrors AES content-hub) |
| Cache key | `sha256([content_hash, taxonomy_version, provider, model, prompt_version])` |
| Score meaning | independent 0–100 ordinal relevance per category (JEV expected level / 4 × 100, half-up); not a probability, not normalised. `details` keeps per-category level, confidence and level probabilities |

## Runtime

* Daily path: after a verified Drive write in `publish_final_md` (and `run_sync` create/update), P03 calls
  `classify_if_needed` for **that source only**. A JEV call happens only for a new source, changed bytes,
  a previous error, or a taxonomy/model/rubric change. Unchanged sources make 0 calls.
* End of batch: entries whose last attempt failed are retried (bounded, 20).
* Provider failure: the previous good scores are kept (`stale: true`, `error` set, `cache_key: null`) and
  retried next run. Publish, staging and mirror are never affected.
* Writes: thread lock + `flock`, fresh re-read, in-place rewrite (same inode, required by Drive Desktop) after
  a durable `classification-index.json.prev` backup, verified readback.

| Variable | Purpose |
|----------|---------|
| `P03_JEV_CLASSIFY_ENABLED` | `1` enables the daily classify hook (**default off** until the owner enables it) |
| `JEV_API_KEY_FILE` | path to the JEV key file (never logged or placed on a command line) |

## Operator commands (not part of the daily path)

```bash
python -m scripts.jev_classify classify --date 2026-10-06 [--dry-run]   # window by date folder
python -m scripts.jev_classify classify --since 2026-10-01 --until 2026-10-06
python -m scripts.jev_classify classify --all --dry-run                 # count would-be calls
python -m scripts.jev_classify classify --path <md> ... [--force]      # explicit reclassify
python -m scripts.jev_classify retry-errors
python -m scripts.jev_classify migrate [--dry-run]                      # one-time v1 → v2 cutover (0 calls)
```

Sources for windows come from P03's own Drive sync state, not a Drive crawl.

## Migration (2026-10-06)

The 1,558-entry intelligence-library v1 index was rewritten in place to v2: 1,542 `video_id` entries
(16 duplicate Drive items with identical bytes merged, newest `classified_at` kept, other Drive ids kept in
`lineage`), 0 unresolvable, field parity exact, every cache key recomputes, 0 JEV calls. A dry run over all
1,558 state rows reports `would_call: 0`.
