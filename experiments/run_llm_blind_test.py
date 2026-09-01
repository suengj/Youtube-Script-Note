#!/usr/bin/env python3
"""One-off blind LLM comparison: GPT-5 Mini vs Gemini 2.5 Flash vs DeepSeek V4 Flash."""

from __future__ import annotations

import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

import stt_function_v3 as stt  # noqa: E402
from main import (  # noqa: E402
    INPUT_PROMPT,
    MAIN_LLM_TOKEN_RANGE,
    TOKEN_INPUT_ROLE,
    TOKEN_QUERY,
)

RUN_ID = "llm_blind_test_20260616"
OUT_DIR = PROJECT_ROOT / "experiments" / RUN_ID
VTT_DIR = OUT_DIR / "vtt"
PRE_DIR = OUT_DIR / "00_preprocessed"
WHISPER_DIR = PROJECT_ROOT / "output_new" / "full"

SAMPLES = [
    {"video_id": "rXcYPHfyyRQ", "title": "PHILOSOPHY - BIOETHICS 2"},
    {"video_id": "dr5z2WvEXBI", "title": "Mexico Will Not Be the Next China"},
]

MODELS = {
    "gpt5_mini": "openai/gpt-5-mini",
    "gemini_flash": "google/gemini-2.5-flash",
    "deepseek_flash": "deepseek/deepseek-v4-flash",
}

# USD per 1M tokens (OpenRouter list prices, June 2026)
PRICING = {
    "openai/gpt-5-mini": (0.25, 2.00),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "deepseek/deepseek-v4-flash": (0.14, 0.28),
    "gpt-5-nano-2025-08-07": (0.05, 0.40),
}


@dataclass
class UsageRecord:
    stage: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_sec: float = 0.0


@dataclass
class UsageTracker:
    records: list[UsageRecord] = field(default_factory=list)

    def wrap(self, client: OpenAI, stage: str) -> OpenAI:
        tracker = self

        class _Completions:
            def __init__(self, inner):
                self._inner = inner

            def create(self, **kwargs):
                model = kwargs.get("model", "")
                t0 = time.time()
                resp = self._inner.create(**kwargs)
                elapsed = time.time() - t0
                usage = getattr(resp, "usage", None)
                tracker.records.append(
                    UsageRecord(
                        stage=stage,
                        model=model,
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        latency_sec=round(elapsed, 2),
                    )
                )
                return resp

        class _Chat:
            def __init__(self, inner_client):
                self.completions = _Completions(inner_client.chat.completions)

        class _Proxy:
            chat = _Chat(client)

        return _Proxy()  # type: ignore[return-value]


def find_vtt(video_id: str) -> Path | None:
    if not VTT_DIR.is_dir():
        return None
    candidates = sorted(VTT_DIR.glob(f"{video_id}*.vtt"))
    return candidates[0] if candidates else None


def find_whisper_fallback(video_id: str) -> Path | None:
    if not WHISPER_DIR.is_dir():
        return None
    for path in WHISPER_DIR.iterdir():
        if path.suffix == ".txt" and f"vid-{video_id}" in path.name and "5-mini" not in path.name:
            if path.stat().st_size > 0:
                return path
    return None


def load_plain_text(video_id: str) -> tuple[str, str]:
    vtt = find_vtt(video_id)
    if vtt:
        text = stt.subtitle_file_to_plain_text(str(vtt))
        if text.strip():
            return text, f"vtt:{vtt.name}"

    whisper = find_whisper_fallback(video_id)
    if whisper:
        text = whisper.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            return text, f"whisper_txt:{whisper.name}"

    raise FileNotFoundError(f"No VTT or Whisper text for {video_id}")


def estimate_cost_usd(records: list[UsageRecord]) -> float:
    total = 0.0
    for r in records:
        pin, pout = PRICING.get(r.model, (0.0, 0.0))
        total += (r.prompt_tokens / 1_000_000) * pin
        total += (r.completion_tokens / 1_000_000) * pout
    return round(total, 6)


def preprocess(concise_path: Path, plain_text: str, openai_client: OpenAI, tracker: UsageTracker) -> str:
    if concise_path.is_file():
        return concise_path.read_text(encoding="utf-8")

    client = tracker.wrap(openai_client, stage="nano_preprocess")
    concise = stt.token_minimizer_chunked(
        TOKEN_INPUT_ROLE,
        TOKEN_QUERY,
        plain_text,
        client,
        model="gpt-5-nano-2025-08-07",
    )
    concise_path.parent.mkdir(parents=True, exist_ok=True)
    concise_path.write_text(concise, encoding="utf-8")
    return concise


def write_scoring_md() -> None:
    lines = [
        "# Blind scoring sheet",
        "",
        "Score each output **A / B / C** per sample (1 = poor, 5 = excellent).",
        "Do **not** open `mapping.json` until scoring is done.",
        "",
        "## Rubric",
        "",
        "1. **Faithfulness** — no major omissions or distortions",
        "2. **Korean quality** — natural wording, not overly formal",
        "3. **Structure** — headings, bullets, tables, `---` separators",
        "4. **Density** — appropriate detail level (~1.3–1.5× input)",
        "5. **Hallucination** — Insights section stays grounded in source",
        "",
        "## Sample 1 (`rXcYPHfyyRQ` — Bioethics)",
        "",
        "| Label | Faith | Korean | Structure | Density | Hallucination | Notes |",
        "|-------|-------|--------|-----------|---------|---------------|-------|",
        "| A | | | | | | |",
        "| B | | | | | | |",
        "| C | | | | | | |",
        "",
        "## Sample 2 (`dr5z2WvEXBI` — Mexico/China)",
        "",
        "| Label | Faith | Korean | Structure | Density | Hallucination | Notes |",
        "|-------|-------|--------|-----------|---------|---------------|-------|",
        "| A | | | | | | |",
        "| B | | | | | | |",
        "| C | | | | | | |",
        "",
        "## Totals (optional)",
        "",
        "| Label | Sum |",
        "|-------|-----|",
        "| A | |",
        "| B | |",
        "| C | |",
        "",
    ]
    (OUT_DIR / "SCORING.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    openai_key = os.environ.get("OPENAI_API_KEY")
    or_key = os.environ.get("OPENROUTER_API_KEY")
    if not openai_key:
        raise SystemExit("OPENAI_API_KEY missing (project .env)")
    if not or_key:
        raise SystemExit("OPENROUTER_API_KEY missing (~/.hermes/profiles/academia/.env)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    VTT_DIR.mkdir(parents=True, exist_ok=True)
    PRE_DIR.mkdir(parents=True, exist_ok=True)

    openai_client = OpenAI(api_key=openai_key)
    or_client = OpenAI(api_key=or_key, base_url="https://openrouter.ai/api/v1")

    tracker = UsageTracker()
    mapping: dict[str, dict] = {}
    run_log: dict = {
        "run_id": RUN_ID,
        "started_at": datetime.now().isoformat(),
        "samples": [],
        "usage": [],
        "estimated_cost_usd": 0.0,
    }

    labels = ["A", "B", "C"]
    model_keys = list(MODELS.keys())

    for idx, sample in enumerate(SAMPLES, start=1):
        video_id = sample["video_id"]
        sample_dir = OUT_DIR / f"sample_{idx:02d}_{video_id}"
        sample_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n=== Sample {idx}: {video_id} ===")
        plain_text, source_kind = load_plain_text(video_id)
        print(f"  Source: {source_kind} ({len(plain_text)} chars)")

        concise_path = PRE_DIR / f"{video_id}_concise.txt"
        concise = preprocess(concise_path, plain_text, openai_client, tracker)
        (sample_dir / "source_concise.txt").write_text(concise, encoding="utf-8")
        print(f"  Concise: {len(concise)} chars, {stt.count_tokens(concise)} tokens")

        shuffle = model_keys.copy()
        random.seed(video_id)
        random.shuffle(shuffle)
        sample_mapping = {label: MODELS[key] for label, key in zip(labels, shuffle)}
        mapping[video_id] = {
            "sample_dir": sample_dir.name,
            "title": sample["title"],
            "source": source_kind,
            "labels": sample_mapping,
        }

        sample_meta = {
            "video_id": video_id,
            "source": source_kind,
            "concise_chars": len(concise),
            "concise_tokens": stt.count_tokens(concise),
            "outputs": [],
        }

        for label, model_key in zip(labels, shuffle):
            model_slug = MODELS[model_key]
            print(f"  Running {label} -> {model_slug}")
            client = tracker.wrap(or_client, stage=f"summarize_{video_id}_{label}")
            t0 = time.time()
            md = stt.summarize_with_chunking(
                transcription=concise,
                filename=sample["title"],
                prompt=INPUT_PROMPT,
                client=client,
                token_range=list(MAIN_LLM_TOKEN_RANGE),
                language="Korean",
                style="Markdown",
                model=model_slug,
            )
            elapsed = round(time.time() - t0, 2)
            out_path = sample_dir / f"{label}.md"
            out_path.write_text(md, encoding="utf-8")
            sample_meta["outputs"].append(
                {
                    "label": label,
                    "model": model_slug,
                    "chars": len(md),
                    "latency_sec": elapsed,
                }
            )
            print(f"    -> {out_path.name} ({len(md)} chars, {elapsed}s)")

        run_log["samples"].append(sample_meta)

    run_log["usage"] = [r.__dict__ for r in tracker.records]
    run_log["estimated_cost_usd"] = estimate_cost_usd(tracker.records)
    run_log["finished_at"] = datetime.now().isoformat()

    (OUT_DIR / "mapping.json").write_text(
        json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "run_meta.json").write_text(
        json.dumps(run_log, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    write_scoring_md()

    print(f"\nDone. Outputs in {OUT_DIR}")
    print(f"Estimated cost: ${run_log['estimated_cost_usd']:.4f}")
    print("Score using SCORING.md before opening mapping.json.")


if __name__ == "__main__":
    main()
