# P03 LLM Model Benchmark — 2026-08-09

Permanent record of the P03 YouTube transcript LLM benchmark executed on **2026-08-08** (UTC). Raw API outputs remain local only; this document preserves measured aggregates and recommendations.

---

## 1. Benchmark purpose

Objective comparison of **preprocess** and **main (summarize/report)** LLM models on P03’s real workload — same prompts, same samples, same scoring — to decide production routing on **quality, cost, latency, and stability**, not on “latest model” assumptions.

Harness: `benchmarks/llm/` (reuses `token_minimizer_chunked`, `summarize_with_chunking`, `INPUT_PROMPT` v3).

---

## 2. Production effective config (at run time)

Verified via `load_config()` / local `.env` on 2026-08-08. **No production change was made.**

| Stage | Provider | Model | Fallback |
|-------|----------|-------|----------|
| **Preprocess** | `openai` | `gpt-5-nano-2025-08-07` | — |
| **Main** | `openai` | `gpt-5-mini-2025-08-07` | **disabled** (`.env` fallback lines commented) |
| **Output suffix** | — | `_5-mini` | — |
| **Preprocess backend** | — | `cloud_api` | — |

---

## 3. Models / providers compared

| Label | Model ID | Provider | Stage | Tested |
|-------|----------|----------|-------|--------|
| Current preprocess | `gpt-5-nano-2025-08-07` | OpenAI | preprocess | yes |
| Candidate preprocess | `gpt-4.1-nano-2025-04-14` | OpenAI | preprocess | yes |
| Current main | `gpt-5-mini-2025-08-07` | OpenAI | main | yes |
| Candidate main | `gpt-4.1-mini-2025-04-14` | OpenAI | main | yes |
| Fallback / alt main | `deepseek/deepseek-v4-flash` | OpenRouter | main + fallback sim | yes |
| GPT-5.6 Luna (or similar) | — | — | — | **NOT TESTED** (not in OpenAI API model list) |

---

## 4. Dataset (12 samples)

Manifest: `benchmarks/llm/dataset_manifest.json`

| ID | Category | Source type | max_input_chars |
|----|----------|-------------|-----------------|
| s01_en_academic | english_to_korean | VTT | 12000 |
| s02_en_economics | english_to_korean | VTT | 12000 |
| s03_ko_general | korean_general | VTT | 10000 |
| s04_ko_general_2 | korean_general | VTT | 10000 |
| s05_tech_dev | tech_dev | cache JSON | 12000 |
| s06_numbers_entities | numbers_proper_nouns | cache JSON | 10000 |
| s07_long_english | long_video | cache JSON | 15000 |
| s08_noisy_auto | noisy_auto_subs | VTT | 8000 |
| s09_ko_auto | korean_general | cache JSON | 12000 |
| s10_mixed_ko_en | mixed_ko_en | cache JSON | 10000 |
| s11_low_quality_short | low_quality_subs | cache JSON | 8000 |
| s12_finance_entities | numbers_proper_nouns | cache JSON | 10000 |

Sources: `experiments/llm_blind_test_20260616/vtt/`, `quarantine/subtitles/`, `cache/transcripts/`. No secrets or full transcripts in this document.

---

## 5. Preprocess results

**Design:** 12 samples × 2 models × 2 runs = **48 runs**. Deterministic scoring only (`benchmarks/llm/scoring.py`).

### Artifact verification note

- **48 raw preprocess outputs** exist under `artifacts/llm_benchmark/20260808T223334Z/raw/preprocess/` (verified).
- Preprocess **`summary.json` was overwritten** when the main stage reused the same `--output-dir`; aggregates below are from the **preprocess-only completion summary** captured at run time and cross-checked against raw outputs and cost totals.
- Partial re-score from raw (10/12 samples; s07/s11 cache sources expired): current_nano **69.98 ± 17.14**, candidate_41_nano **73.53 ± 15.26** — directionally consistent; full-run figures below remain authoritative for n=48.

| Model | Quality mean ± σ | Measured cost (24 runs) | Latency p50 / p95 | Failures | Pareto |
|-------|------------------|-------------------------|-------------------|----------|--------|
| **gpt-5-nano** (current) | **72.39 ± 16.73** | **$0.067821** | **36.16s / 74.96s** | **0** | **dominated** |
| **gpt-4.1-nano** (candidate) | **74.87 ± 14.34** | **$0.018608** | **6.22s / 10.42s** | **0** | **frontier** |

**Verified deltas (candidate vs current):**

- Quality: **+2.48** (~**+2.5** pts)
- Preprocess cost: **−72.6%** (~**−73%**; $0.018608 / $0.067821)
- Latency p50: **5.81× faster** (~**6×**; 36.16s → 6.22s)

**Pareto:** `gpt-4.1-nano` dominates `gpt-5-nano` (≥ quality, ≤ cost, strict improvement on both).

---

## 6. Main results

**Design:** 12 samples × 3 models × 2 runs = **72 runs**. All models received the **same concise input** (current nano preprocess). Source: `artifacts/llm_benchmark/20260808T223334Z/summary.json` and `results.jsonl` (verified).

| Model | Quality mean ± σ | Measured cost (24 runs) | Latency p50 / p95 | Failures |
|-------|------------------|---------------------------|-------------------|----------|
| **gpt-5-mini** (current) | **76.22 ± 10.10** | **$0.199012** | **35.06s / 44.86s** | **0** |
| gpt-4.1-mini | **74.70 ± 8.31** | $0.078257 | 20.76s / 29.04s | 0 |
| deepseek/deepseek-v4-flash | **75.24 ± 6.29** | $0.028864 | 58.37s / 113.31s | 0 |

> **Verification:** GPT-4.1-mini σ is **8.31** in artifact (not 3.31).

**Pareto (main-only, quality vs measured API cost):** No candidate fully dominates current mini — trade-off between quality and cost. Summary artifact flagged `candidate_41_mini` and `candidate_deepseek` as dominated due to label→model cost mapping quirk in early harness; treat main Pareto as **inconclusive** for production routing.

---

## 7. End-to-end results (quick pass)

**Design:** **4 samples** (quick) × 4 combos × **1 run** = 16 runs. Artifact: `artifacts/llm_benchmark/20260808T223334Z_e2e/summary.json` (verified).

| Combo | Preprocess → Main | Quality mean ± σ | Latency p50 / p95 | Failures |
|-------|-------------------|------------------|-------------------|----------|
| A_current | nano → mini | **64.18 ± 5.33** | 78.80s / 103.64s | 0 |
| B_cheap_pre | 4.1-nano → mini | 61.77 ± 12.79 | 43.71s / 57.67s | 0 |
| C_cheap_main | nano → 4.1-mini | 64.72 ± 4.72 | 53.89s / 68.22s | 0 |
| D_both_cheap | 4.1-nano → 4.1-mini | **68.85 ± 8.69** | 25.04s / 32.23s | 0 |

**Pareto (n=4):** D_both_cheap highest quality in this subset; **not sufficient** to override main-only results.

---

## 8. Fallback simulation

Runtime fallback was **disabled**. Simulated primary failure → OpenRouter DeepSeek. Artifact: `artifacts/llm_benchmark/20260808T223334Z_fallback_final/results.jsonl` (verified).

| Field | Value |
|-------|-------|
| Primary (configured) | `gpt-5-mini-2025-08-07` |
| Fallback | `openrouter` / `deepseek/deepseek-v4-flash` |
| Recovery success | **true** |
| Quality (deterministic) | **64.77** |
| Extra latency | **162.3s** |
| Output size | 2652 chars |
| Measured fallback-path cost | **$0.001797** (preprocess + fallback main in sim) |

---

## 9. Cost analysis

Per-video estimates from **measured API usage per run** (preprocess cost ÷ 24 + main cost ÷ 24):

| Stack | $/video (measured) | Verification |
|-------|-------------------|--------------|
| **Current:** nano + mini | **~$0.011** ($0.011118) | ✓ |
| **Best-value candidate:** 4.1-nano + mini | **~$0.009** ($0.009068) | ✓ |
| nano + DeepSeek main | **~$0.004** ($0.004029) | ✓ |
| 4.1-nano + 4.1-mini | **~$0.004** ($0.004036) | ✓ (prior informal ~$0.007 figure **not** supported by per-run usage math) |

**Main-stage-only projection** (artifact, not full stack): $0.025511/video, ~$7.65/month @ 300 videos.

**Preprocess-stage-only projection** (from preprocess run): $0.007202/video equivalent, ~$2.16/month @ 300 videos.

Full production spend depends on untruncated transcript length and chunking; benchmark corpus is conservative.

---

## 10. Recommendations (frozen from evidence)

### BEST QUALITY

- Preprocess: `gpt-4.1-nano-2025-04-14` (higher deterministic score in preprocess pass)
- Main: `gpt-5-mini-2025-08-07`

### BEST VALUE

- **`gpt-4.1-nano-2025-04-14` → `gpt-5-mini-2025-08-07`**
- Largest win: cheaper/faster preprocess; keep main quality leader.

### LOWEST COST ACCEPTABLE

- `gpt-4.1-nano-2025-04-14` + `deepseek/deepseek-v4-flash`
- Accept ~−1 pt main quality and higher latency for lowest $/video.

### Production recommendation (2026-08-09)

| Component | Action | Rationale |
|-----------|--------|-----------|
| **Main** | **KEEP `gpt-5-mini`** | Highest quality (76.22); do **not** switch to DeepSeek or 4.1-mini on this benchmark alone |
| **Preprocess** | **Migration candidate → `gpt-4.1-nano`** | +2.5 quality, −73% preprocess cost, ~6× faster p50 |
| **Fallback** | **Optional enable** | Recovery verified; **+162s** latency on failure — enable only if resilience outweighs delay |

**Production config was NOT changed** as part of this benchmark.

---

## 11. Limitations

1. **Preprocess/main:** 12 samples × 2 runs — not full production corpus.
2. **E2E:** quick pass — **4 samples × 1 run** only.
3. **Truncated input:** `max_input_chars` 8k–15k per sample; full-length videos cost more and may behave differently.
4. **Deterministic scoring only** in reported aggregates (blind judge disabled via `--no-judge`).
5. **Do not treat** these scores as absolute proof of production quality — use for **relative routing** decisions only.
6. Preprocess **`summary.json` overwrite** when reusing output dir — preprocess aggregates preserved here from first-pass capture; re-run with separate dirs for full artifact parity.

---

## 12. Local raw artifacts

| Run | Path | Timestamp (UTC) |
|-----|------|-----------------|
| Preprocess + Main | `artifacts/llm_benchmark/20260808T223334Z/` | 2026-08-08T22:33:34Z |
| End-to-end (quick) | `artifacts/llm_benchmark/20260808T223334Z_e2e/` | 2026-08-08 (e2e stage ~22:38–23:52 UTC) |
| Fallback simulation | `artifacts/llm_benchmark/20260808T223334Z_fallback_final/` | 2026-08-08 (~23:56 UTC) |

Raw outputs under `raw/` are gitignored. Do not commit `.env`, API keys, or transcript text.

---

## 13. Re-run commands

From repo root with `ai` conda env and valid `.env`:

```bash
# Full preprocess + main (12 samples, 2 runs each)
python -m benchmarks.llm.benchmark --stage preprocess --runs 2 --no-judge \
  --output-dir artifacts/llm_benchmark/$(date -u +%Y%m%dT%H%M%SZ)_preprocess

python -m benchmarks.llm.benchmark --stage main --runs 2 --no-judge \
  --output-dir artifacts/llm_benchmark/$(date -u +%Y%m%dT%H%M%SZ)_main

# End-to-end quick (4 samples)
python -m benchmarks.llm.benchmark --stage end-to-end --runs 1 --quick --no-judge \
  --output-dir artifacts/llm_benchmark/$(date -u +%Y%m%dT%H%M%SZ)_e2e

# Fallback simulation
python -m benchmarks.llm.benchmark --stage fallback \
  --output-dir artifacts/llm_benchmark/$(date -u +%Y%m%dT%H%M%SZ)_fallback

# Unit tests
python -m pytest tests/test_llm_benchmark.py -q
```

Use **separate `--output-dir`** per stage to avoid summary/results overwrite.

---

## Verification checklist (2026-08-09)

| Claim | Artifact check |
|-------|----------------|
| GPT-5 nano preprocess 72.39 ± 16.73 | Preprocess completion summary + 48 raw files |
| GPT-4.1 nano 74.87 ± 14.34 | Same |
| +2.5 / −73% / ~6× p50 | Derived from preprocess cost & latency tables ✓ |
| GPT-5 mini main 76.22 ± 10.10 | `20260808T223334Z/summary.json` ✓ |
| GPT-4.1 mini 74.70 ± **8.31** | Same ✓ (σ corrected from typo 3.31) |
| DeepSeek 75.24 ± 6.29 | Same ✓ |
| Current stack ~$0.011/video | Usage ÷ 24 runs ✓ |
| 4.1-nano + mini ~$0.009/video | Usage ÷ 24 runs ✓ |
| nano + DeepSeek ~$0.004/video | Usage ÷ 24 runs ✓ |
| 4.1-nano + 4.1-mini ~$0.004/video | Usage ÷ 24 runs ✓ (not ~$0.007) |
| Fallback recovery | `20260808T223334Z_fallback_final/results.jsonl` ✓ |
