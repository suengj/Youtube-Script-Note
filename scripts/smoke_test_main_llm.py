#!/usr/bin/env python3
"""Smoke test: primary main LLM and optional fallback on cached concise text."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

import stt_function_v3 as stt  # noqa: E402
from main import INPUT_PROMPT, MAIN_LLM_TOKEN_RANGE, MainLlmConfig, initialize_clients, load_config  # noqa: E402

CONCISE_CANDIDATES = [
    PROJECT_ROOT / "benchmarks/llm/fixtures/concise/bench_en_academic.txt",
    PROJECT_ROOT / "benchmarks/llm/fixtures/concise/bench_en_economics.txt",
]


def _run_summarize(main_llm: MainLlmConfig, concise: str, label: str) -> tuple[str, float]:
    t0 = time.time()
    md = main_llm.summarize(
        transcription=concise,
        filename=f"SMOKE_TEST_{label}",
        prompt=INPUT_PROMPT,
        token_range=list(MAIN_LLM_TOKEN_RANGE),
        language="Korean",
        style="Markdown",
    )
    return md, round(time.time() - t0, 2)


def main() -> int:
    print("=== Smoke test: main LLM primary + fallback ===")
    config = load_config()
    print("BASE_PATH:", config["BASE_PATH"])
    print("DATA_ROOT:", config["DATA_ROOT"])
    print(
        "PRIMARY:",
        config.get("MAIN_LLM_MODEL"),
        f"({config.get('MAIN_LLM_PROVIDER')})",
        "suffix=_" + config.get("MAIN_LLM_OUTPUT_SUFFIX", ""),
    )
    if config.get("MAIN_LLM_FALLBACK_MODEL"):
        print(
            "FALLBACK:",
            config.get("MAIN_LLM_FALLBACK_MODEL"),
            f"({config.get('MAIN_LLM_FALLBACK_PROVIDER')})",
        )

    _, _, main_llm = initialize_clients(config)
    assert main_llm.primary_client is not None

    concise_path = next((p for p in CONCISE_CANDIDATES if p.is_file()), None)
    if not concise_path:
        print("FAIL: no benchmark concise fixture; check benchmarks/llm/fixtures/concise/.")
        return 1

    concise = concise_path.read_text(encoding="utf-8")
    print(f"Input: {concise_path.name} ({len(concise)} chars, {stt.count_tokens(concise)} tokens)")

    md, elapsed = _run_summarize(main_llm, concise, "PRIMARY")
    out_dir = PROJECT_ROOT / "logs" / "smoke_test"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = config.get("MAIN_LLM_OUTPUT_SUFFIX", "5-mini")
    out_path = out_dir / f"smoke_primary_{suffix}_{int(time.time())}.md"
    out_path.write_text(md, encoding="utf-8")

    ok_primary = len(md) > 200 and "##" in md
    print(f"Primary output: {len(md)} chars, {elapsed}s -> {out_path}")
    print("PRIMARY:", "PASS" if ok_primary else "FAIL")

    if not main_llm.has_fallback:
        print("FALLBACK: skipped (not configured)")
        return 0 if ok_primary else 1

    # Force primary failure to verify fallback path without changing .env permanently.
    broken = MainLlmConfig(
        primary_client=main_llm.primary_client,
        primary_model="openai/this-model-should-not-exist-smoke-test",
        primary_provider=main_llm.primary_provider,
        fallback_client=main_llm.fallback_client,
        fallback_model=main_llm.fallback_model,
        fallback_provider=main_llm.fallback_provider,
    )
    try:
        fb_md, fb_elapsed = _run_summarize(broken, concise, "FALLBACK")
    except Exception as exc:
        print(f"FALLBACK: FAIL ({exc})")
        return 1

    fb_path = out_dir / f"smoke_fallback_{suffix}_{int(time.time())}.md"
    fb_path.write_text(fb_md, encoding="utf-8")
    ok_fallback = len(fb_md) > 200 and "##" in fb_md
    print(f"Fallback output: {len(fb_md)} chars, {fb_elapsed}s -> {fb_path}")
    print("FALLBACK:", "PASS" if ok_fallback else "FAIL")

    return 0 if ok_primary and ok_fallback else 1


if __name__ == "__main__":
    raise SystemExit(main())
