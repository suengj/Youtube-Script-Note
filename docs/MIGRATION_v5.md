# P03 v4.2 to v5.0 migration

P03 v5.0 adds an opt-in single-pass transcript-to-Markdown path using the GPT-6 Luna Responses API. It skips Nano preprocessing and Mini chunk summarization, preserves the existing Markdown renderer and `format_version: 4.1`, and records token usage metadata under `<DATA_ROOT>/logs/llm_usage.jsonl`.

The shipped code default remains `LLM_PIPELINE_MODE=legacy_two_stage` pending owner approval. To evaluate the direct path, set `LLM_PIPELINE_MODE=direct_luna`, choose `DIRECT_LLM_REASONING_EFFORT=low|medium|high`, and configure the direct input/output token ceilings. Inputs above the configured ceiling fail for manual review instead of being chunked.

Rollback is `LLM_PIPELINE_MODE=legacy_two_stage` while the GPT-5 Nano and Mini APIs remain available. `MAIN_LLM_OUTPUT_SUFFIX` can continue to pin a preferred filename suffix; otherwise direct mode uses `luna-<effort>`.

The offline benchmark runner is documented in [benchmarks/llm/README.md](../benchmarks/llm/README.md). No real transcript content, API keys, or personal paths belong in tracked files.
