#!/usr/bin/env python3
"""Pilot INPUT_PROMPT on benchmark concise fixtures (optional dev script)."""

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

FIXTURE_DIR = PROJECT_ROOT / "benchmarks" / "llm" / "fixtures" / "concise"
OUT_DIR = PROJECT_ROOT / "experiments" / "mobile_md_pilot"
SAMPLES = [
    ("bench_en_academic", "Bioethics lecture (synthetic)"),
    ("bench_en_economics", "Supply chain economics (synthetic)"),
]


def run_one(vid: str, title: str, main_llm: MainLlmConfig) -> dict:
    concise_path = FIXTURE_DIR / f"{vid}.txt"
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
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(response, encoding="utf-8")
    return {
        "path": str(out_path.relative_to(PROJECT_ROOT)),
        "format_version": "4.1",
        "title": title,
        "chars": len(response),
        "tokens": stt.count_tokens(response),
    }


def main() -> int:
    config = load_config()
    _, _, main_llm = initialize_clients(config)
    meta = {}
    for vid, title in SAMPLES:
        meta[vid] = run_one(vid, title, main_llm)
        print(f"Wrote {meta[vid]['path']} ({meta[vid]['chars']} chars)")
    (OUT_DIR / "pilot_meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
