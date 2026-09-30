# P03 LLM Benchmark

Reusable harness to compare preprocess and main LLM models on **synthetic transcript workloads** shipped in `benchmarks/llm/fixtures/`.

## v5 single-pass benchmark (SUE-1263)

The runner compares arm A (production Nano preprocessing + Mini summarization) with arms B/C/D (GPT-6 Luna Responses at low/medium/high effort, identical prompt). Each run freezes exactly three source files and writes per-request usage, outputs, a provenance manifest, and a category-by-arm comparison.

```bash
python -m benchmarks.llm.v5_single_pass --manifest <json> --out artifacts/llm_benchmark/v5_single_pass/<RUN_ID> --dry-run
python -m benchmarks.llm.v5_single_pass --manifest <json> --out artifacts/llm_benchmark/v5_single_pass/<RUN_ID> --arms A,B,C,D
```

`--dry-run` uses a fake client and makes no network calls. Completed arm outputs are skipped on rerun. The manifest must contain exactly three sources with `category`, `video_id`, `title`, `source_path`, `language`, and `source_type`. Live mode incurs API usage; use only approved benchmark material.

## Prerequisites

- `.env` with `OPENAI_API_KEY` (required)
- `OPENROUTER_API_KEY` (optional; needed for DeepSeek main/fallback candidates)

## Usage

```bash
# Full audit (all stages, 12 samples, 2 runs each — may take 30+ min and incur API cost)
python -m benchmarks.llm.benchmark --stage all --runs 2

# Quick smoke (4 samples)
python -m benchmarks.llm.benchmark --stage preprocess --quick --runs 1

python -m benchmarks.llm.benchmark --stage main --quick --runs 2
python -m benchmarks.llm.benchmark --stage end-to-end --quick --runs 1
python -m benchmarks.llm.benchmark --stage fallback
```

## Artifacts

Outputs go to `artifacts/llm_benchmark/<timestamp>/`:

| File | Contents |
|------|----------|
| `manifest.json` | Dataset + runtime config snapshot |
| `results.jsonl` | Per-run scores, latency, flags |
| `summary.json` / `summary.csv` | Aggregated quality, cost, Pareto |
| `report.md` | Human-readable summary |
| `raw/` | Model outputs (not for git) |

## Design

- **Production reuse:** `token_minimizer_chunked`, `summarize_with_chunking`, `INPUT_PROMPT`, `build_token_query`
- **Deterministic scoring:** numbers/entities, retention, markdown sections, anti-verbosity
- **Blind judge (optional):** fixed judge model; candidate labels only
- **Fallback test:** mocked primary failure → OpenRouter fallback path

## Models compared (default manifest)

| Stage | Current | Candidates |
|-------|---------|------------|
| Preprocess | `gpt-5-nano-2025-08-07` | `gpt-4.1-nano-2025-04-14` |
| Main | `gpt-5-mini-2025-08-07` | `gpt-4.1-mini-2025-04-14`, `deepseek/deepseek-v4-flash` |

Edit `dataset_manifest.json` to change candidates or samples.

## Tests

```bash
python -m pytest tests/test_llm_benchmark.py -q
```
