# UPDATE_LOG — INPUT_PROMPT v3 (2026-07-05)

> **Version:** `4.1.2` · prompt-only · no launchd redeploy

## Problem

Recent `_dS4f` MD notes were short and structurally flat. v2 `INPUT_PROMPT` emphasized mobile brevity (“limited time”) over substantive reorganization.

## Changes

| Area | Change |
|------|--------|
| `main.py` `INPUT_PROMPT` | **v3:** reorganize-first; 3~6 topic `##` sections with 2+ concrete points each; prefer restructuring over deleting |
| `main.py` | `MAIN_LLM_TOKEN_RANGE = (1.5, 2.0)` (was 1.3–1.5) |
| `stt_function_v3.py` | `MERGE_SUMMARY_PROMPT` aligned with v3 depth for chunked long videos |
| Pilot/smoke/retry scripts | Import shared `MAIN_LLM_TOKEN_RANGE` |

**Unchanged:** v4.1 section order (한눈에 보기 → body → callouts → Tags → 용어), A4 grounding tags, `md_mobile_utils.py`, nano retention in `config.py`.

## Validation

```bash
python -m py_compile main.py stt_function_v3.py
/opt/homebrew/Caskroom/miniforge/base/envs/ai/bin/python scripts/smoke_test_main_llm.py
```

Compare pilot output: H2 count, examples preserved, `len(MD)/len(concise)`.

## Rollout

- Applies to **new pipeline runs** only (launchd picks up on next `main.py` run).
- No bulk re-summarize of existing notes (per user workflow).

## LLM stack (2026-07-05)

| Stage | Model | Env |
|-------|-------|-----|
| Preprocess | `gpt-5-nano-2025-08-07` | `PREPROCESS_LLM_MODEL` |
| Summarize | `gpt-5-mini-2025-08-07` (OpenAI) | `MAIN_LLM_PROVIDER=openai`, `MAIN_LLM_MODEL` |
| Suffix | `_5-mini` | `MAIN_LLM_OUTPUT_SUFFIX=5-mini` |
| Fallback | off | — |

**Suffix rule:** `gpt-5-mini` → `5-mini` · `deepseek/*` → `dS4f` (set `MAIN_LLM_OUTPUT_SUFFIX` when switching models).
