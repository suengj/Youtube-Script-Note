# -*- coding: utf-8 -*-
"""Token usage and cost estimation for LLM benchmark runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# USD per 1M tokens (input, output). Updated 2026-08 from provider pricing pages.
# OpenRouter models use OpenRouter list prices where applicable.
PRICE_PER_1M: Dict[str, Tuple[float, float]] = {
    "gpt-5-nano-2025-08-07": (0.05, 0.40),
    "gpt-5-mini-2025-08-07": (0.25, 2.00),
    "gpt-4.1-nano-2025-04-14": (0.10, 0.40),
    "gpt-4.1-mini-2025-04-14": (0.40, 1.60),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    "deepseek/deepseek-v4-flash": (0.14, 0.28),
    "openai/gpt-5-mini": (0.25, 2.00),
}


@dataclass
class UsageRecord:
    provider: str
    model: str
    stage: str
    sample_id: str
    run_index: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    total_tokens: int = 0
    latency_sec: float = 0.0
    retry_count: int = 0
    success: bool = True
    error: str = ""
    extra_cost_usd: float = 0.0  # e.g. fallback duplicate billing

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "stage": self.stage,
            "sample_id": self.sample_id,
            "run_index": self.run_index,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "total_tokens": self.total_tokens,
            "latency_sec": round(self.latency_sec, 3),
            "retry_count": self.retry_count,
            "success": self.success,
            "error": self.error,
            "est_cost_usd": round(estimate_cost_usd(self.model, self.prompt_tokens, self.completion_tokens), 8),
            "extra_cost_usd": round(self.extra_cost_usd, 8),
        }


@dataclass
class UsageTracker:
    records: List[UsageRecord] = field(default_factory=list)

    def add(self, record: UsageRecord) -> None:
        self.records.append(record)


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = PRICE_PER_1M.get(model, (0.0, 0.0))
    return (prompt_tokens / 1_000_000) * pin + (completion_tokens / 1_000_000) * pout


def aggregate_costs(records: List[UsageRecord]) -> Dict[str, float]:
    total = 0.0
    by_model: Dict[str, float] = {}
    for r in records:
        if not r.success:
            continue
        c = estimate_cost_usd(r.model, r.prompt_tokens, r.completion_tokens) + r.extra_cost_usd
        total += c
        by_model[r.model] = by_model.get(r.model, 0.0) + c
    return {"total_usd": round(total, 6), "by_model": {k: round(v, 6) for k, v in by_model.items()}}


def cost_projection(total_usd: float, n_videos: int) -> Dict[str, float]:
    per = total_usd / max(n_videos, 1)
    return {
        "per_video_usd": round(per, 6),
        "per_10_videos_usd": round(per * 10, 4),
        "per_100_videos_usd": round(per * 100, 2),
        "monthly_300_videos_usd": round(per * 300, 2),
    }


def pareto_dominates(
    quality_a: float,
    cost_a: float,
    quality_b: float,
    cost_b: float,
) -> bool:
    """True if A dominates B (>= quality, <= cost, strict on at least one)."""
    if quality_a >= quality_b and cost_a <= cost_b:
        return quality_a > quality_b or cost_a < cost_b
    return False
