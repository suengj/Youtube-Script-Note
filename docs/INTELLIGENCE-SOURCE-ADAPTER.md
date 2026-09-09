# P03 as a Learning Intelligence Source Adapter

Status: **implemented and certified (SUE-733)**  
Implementation: `scripts/intelligence_source_adapter.py` · Tests: `scripts/test_intelligence_source_adapter.py`  
Related systems: `suengj/reference-library`, `suengj/ai-editorial-system`

## Decision

P03 remains the **YouTube discovery / transcript / summary producer**. It does not become a generic blog/news crawler.

The recurring Learning Intelligence workflow consumes P03 as one source adapter alongside independent web/RSS retrieval for blogs and institutional sources.

```text
P03 YouTube pipeline ───────┐
                           ├─ Daily Intelligence Agent → reference-library/intelligence
Web / RSS / primary web ───┘
```

This preserves P03's existing platform-specific strengths and avoids coupling unrelated web crawling into a macOS/MLX-oriented YouTube runtime.

## Existing assets to reuse

Prefer the existing bounded/indexed surfaces rather than scanning the Markdown corpus:

- finalized Markdown metadata / provenance;
- note catalog where available;
- daily digest;
- Drive Desktop finalized-summary handoff where enabled;
- stable video identity and canonical YouTube URL.

The Intelligence workflow must not recursively scan the full historical output directory on every run.

## Required source-adapter record

A bounded P03 export/handoff should expose enough metadata for an upstream agent to decide whether a video contributes new learning without treating the summary as primary evidence.

Minimum fields:

| Field | Meaning |
| --- | --- |
| `source_type` | constant `youtube` |
| `video_id` | durable P03/YouTube identity |
| `source_url` | canonical YouTube URL |
| `channel` | channel/publisher |
| `title` | video title |
| `upload_date` | source date when known |
| `processed_at` | P03 processing time |
| `summary_ref` | path/URI to finalized Markdown or exported summary |
| `summary_hash` | optional integrity/hash field when available |
| `tldr` | bounded discovery summary |
| `tags` | topic routing metadata |
| `format_version` | P03 output/adapter contract version |

Existing fields may satisfy this contract under different names; do not create duplicate state merely to rename them.

## Consumer rules

The Daily Intelligence Agent should:

1. read only records newer than the previous successful checkpoint or within a bounded recovery window;
2. deduplicate on durable video identity;
3. use `tldr`/summary for discovery and topic clustering;
4. follow the original video or stronger primary evidence for important factual claims when necessary;
5. preserve P03 source identity in every derived Daily Brief / Knowledge / Dossier artifact that uses it.

## Responsibility boundary

P03 owns:

- YouTube discovery;
- subtitle/audio acquisition;
- transcription;
- transcript preprocessing;
- structured summary generation;
- durable YouTube provenance;
- bounded export/handoff of recent finalized items.

P03 does **not** own:

- A+/A blog source ranking;
- cross-source impact or novelty ranking;
- the user's Daily Learning Brief;
- cumulative Knowledge Map decisions;
- Article Candidate selection;
- Topic Dossier synthesis;
- editorial framing or publication.

Those belong to the higher-level Intelligence / Editorial workflow.

## Implementation rule

Before adding a new exporter, inspect whether the existing note catalog / digest / Drive sync already provides the required fields and bounded incremental read. Prefer a compatibility adapter over a second parallel catalog.

Any new runtime code must remain optional and fail without disrupting the canonical YouTube transcription pipeline.

## Implementation

`scripts/intelligence_source_adapter.py` is a compatibility view over the existing
`index/note_catalog.jsonl` surface. It creates no second catalog: contract fields are
remapped from catalog fields (`vid`, `source_url`, `channel`, `title`, `upload_date`,
`transcript_date`, `tldr`, `tags`, `md_path_rel`), and each record keeps a `provenance`
block naming the originating catalog and row source.

```bash
# bounded preview
python scripts/intelligence_source_adapter.py --since-days 3 --dry-run

# emit records and advance the checkpoint
python scripts/intelligence_source_adapter.py --output /path/p03_intel.jsonl

# re-emit a window for recovery without moving the checkpoint
python scripts/intelligence_source_adapter.py --replay --since-days 7

# disable the handoff; the canonical P03 runtime is unaffected
P03_INTELLIGENCE_ADAPTER=off python scripts/intelligence_source_adapter.py
```

Bounding and idempotency:

- the window starts at the stored checkpoint minus a small recovery slack
  (`index/intelligence_adapter_state.json`), or at an explicit `--since-days` / `--since`;
- the catalog is streamed once and filtered by `transcript_date`; the historical Markdown
  corpus is never walked, and Markdown files are opened only for the opt-in
  `--with-summary-hash` pass;
- emitted video ids are recorded in a ledger pruned to a 30-day retention window, so a
  rerun over an unchanged catalog emits nothing;
- a rerun that emits nothing **keeps the existing export** rather than truncating it, so a
  consumer pointed at a stable `--output` path never silently sees zero records; pass
  `--allow-empty-overwrite` to empty it deliberately;
- duplicate rows for one video (different language or summariser suffix) collapse to a
  single discovery record;
- `--limit` truncates the *oldest* eligible rows, so a truncated run deliberately does not
  advance the checkpoint. The deferred rows are emitted by the next run instead of falling
  outside its window; `deferred_to_next_run` and `checkpoint_held_by_limit` report this.
  The oldest deferred date is persisted as `pending_window_start`, so the next run widens
  its window back to the backlog even when it is invoked with no arguments;
- the emitted-id ledger is never pruned below the start of the window actually read. A
  normal run reads a few days, so the 30-day retention dominates and the ledger stays
  small; a deliberately wide `--since` widens retention to match, which prevents an id
  being pruned and then re-emitted forever while a backlog drains behind it;
- a missing or unreadable catalog changes nothing: the checkpoint stays put and a pending
  backlog is preserved rather than being mistaken for a completed drain.

`evidence_role` is fixed to `derived-summary`: a P03 summary is a discovery and clustering
input, never primary evidence, unless the video itself is the event being analysed.

## Acceptance direction

The future implementation is acceptable when:

- a real bounded sample of recent P03 items can be consumed without full-corpus scanning;
- reruns are idempotent;
- provenance survives end-to-end into a sample Intelligence artifact;
- no blog/RSS crawler is added to P03;
- existing P03 transcription/summarization tests remain green;
- disabling the intelligence handoff leaves the existing P03 runtime behavior unchanged.
