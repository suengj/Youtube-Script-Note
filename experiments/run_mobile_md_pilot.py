#!/usr/bin/env python3
"""Pilot INPUT_PROMPT v2 on preprocessed concise texts (Phase 1a)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

import stt_function_v3 as stt  # noqa: E402
from main import INPUT_PROMPT, MAIN_LLM_TOKEN_RANGE, MainLlmConfig, initialize_clients, load_config  # noqa: E402

PRE_DIR = PROJECT_ROOT / "experiments" / "llm_blind_test_20260616" / "00_preprocessed"
OUT_DIR = PROJECT_ROOT / "experiments" / "mobile_md_pilot"
SAMPLES = [
    ("rXcYPHfyyRQ", "PHILOSOPHY - BIOETHICS 2"),
    ("dr5z2WvEXBI", "Mexico Will Not Be the Next China"),
]


def run_one(vid: str, title: str, main_llm: MainLlmConfig) -> dict:
    concise_path = PRE_DIR / f"{vid}_concise.txt"
    if not concise_path.is_file():
        raise FileNotFoundError(concise_path)
    text = concise_path.read_text(encoding="utf-8-sig")
    response = main_llm.summarize(
        transcription=text,
        filename=title,
        prompt=INPUT_PROMPT,
        token_range=list(MAIN_LLM_TOKEN_RANGE),
        language="Korean",
        style="Markdown",
    )
    out_path = OUT_DIR / f"{vid}_v2.md"
    out_path.write_text(response, encoding="utf-8-sig")
    checks = {
        "has_tldr": "## 한눈에 보기" in response,
        "has_insights_callout": "> [!note]-" in response and "Insights" in response,
        "has_tags": "## Tags" in response,
        "no_mermaid": "```mermaid" not in response and "mermaid" not in response.lower(),
        "no_gaps_section": "부족한 점" not in response,
    }
    return {"vid": vid, "path": str(out_path), "len": len(response), "checks": checks}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config()
    _, _, main_llm = initialize_clients(config)
    results = []
    for vid, title in SAMPLES:
        print(f"Pilot: {vid} …")
        results.append(run_one(vid, title, main_llm))
    meta_path = OUT_DIR / "pilot_meta.json"
    meta_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
