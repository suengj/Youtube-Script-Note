# -*- coding: utf-8 -*-
"""Unit tests for LLM benchmark scoring, cost, and fallback helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.llm.cost import (
    aggregate_costs,
    cost_projection,
    estimate_cost_usd,
    pareto_dominates,
    UsageRecord,
)
from benchmarks.llm.scoring import (
    score_main_deterministic,
    score_preprocess_deterministic,
)
from benchmarks.llm.runner import FailingPrimaryClient, load_manifest


def test_preprocess_score_retention_in_band():
    src = "음 그래서 GPT-5-nano는 2025년에 100달러를 절약합니다. OpenAI API key는 secret입니다."
    out = "그래서 GPT-5-nano는 2025년에 100달러를 절약합니다. OpenAI API key는 secret입니다."
    s = score_preprocess_deterministic(src, out, retention_target=(60, 95))
    assert s.deterministic >= 70
    assert s.details["numbers_entities"] > 0


def test_preprocess_empty_output():
    s = score_preprocess_deterministic("hello world", "")
    assert s.deterministic == 0
    assert "empty_output" in s.flags


def test_main_required_sections():
    concise = "Jensen Huang discussed NVIDIA H100 GPUs and 2024 revenue of 60 billion dollars."
    md = """# Title

## 한눈에 보기
- [확정] NVIDIA H100 매출

## 본문
### GPU
- 2024 revenue 60 billion dollars

> [!note]- Insights
> - [외부지식] AI chip market context

> [!note]- Key Takeaways
> - watch NVIDIA supply chain

## Tags
- nvidia
"""
    s = score_main_deterministic(concise, md)
    assert s.deterministic >= 60
    assert s.details["markdown_structure"] >= 10


def test_cost_estimate_known_model():
    c = estimate_cost_usd("gpt-5-nano-2025-08-07", 1_000_000, 1_000_000)
    assert abs(c - 0.45) < 0.001


def test_aggregate_and_projection():
    records = [
        UsageRecord("openai", "gpt-5-nano-2025-08-07", "preprocess", "s1", 0, 1000, 500),
        UsageRecord("openai", "gpt-5-mini-2025-08-07", "main", "s1", 0, 2000, 1000),
    ]
    agg = aggregate_costs(records)
    assert agg["total_usd"] > 0
    proj = cost_projection(agg["total_usd"], 1)
    assert proj["per_10_videos_usd"] == pytest.approx(proj["per_video_usd"] * 10)


def test_pareto_dominates():
    assert pareto_dominates(90, 1.0, 80, 2.0)
    assert not pareto_dominates(80, 2.0, 90, 1.0)
    assert not pareto_dominates(90, 2.0, 80, 1.0)


def test_manifest_loads():
    path = Path(__file__).resolve().parents[1] / "benchmarks" / "llm" / "dataset_manifest.json"
    m = load_manifest(path)
    assert len(m["samples"]) >= 10
    assert m["model_candidates"]["preprocess"]


def test_failing_primary_raises():
    with pytest.raises(Exception):
        FailingPrimaryClient.chat.completions.create(model="x", messages=[])


def test_scoring_meta_commentary_penalty():
    src = "Simple transcript about Python 3.12 release."
    out = "Sure, here is the revised text: Python 3.12 release notes."
    s = score_preprocess_deterministic(src, out)
    assert s.details.get("hallucination_guard", 20) == 0
