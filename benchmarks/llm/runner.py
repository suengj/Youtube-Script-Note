# -*- coding: utf-8 -*-
"""Benchmark runner — reuses production preprocess/main pipeline functions."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from openai import OpenAI

from benchmarks.llm.cost import UsageRecord, UsageTracker, estimate_cost_usd
from benchmarks.llm.scoring import (
    MAIN_JUDGE_RUBRIC,
    PREPROCESS_JUDGE_RUBRIC,
    ScoreBreakdown,
    build_blind_judge_prompt,
    score_main_deterministic,
    score_preprocess_deterministic,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ModelSpec:
    label: str
    provider: str  # openai | openrouter
    model: str
    stage: str  # preprocess | main | both


def load_manifest(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_sample_text(sample: Dict[str, Any], project_root: Path) -> str:
    rel = sample["source_path"]
    p = project_root / rel
    if not p.is_file():
        raise FileNotFoundError(f"Sample source missing: {p}")
    if p.suffix.lower() == ".vtt":
        import stt_function_v3 as stt

        text = stt.subtitle_file_to_plain_text(str(p))
    elif p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        text = data.get("text") or data.get("transcript") or ""
    else:
        text = p.read_text(encoding="utf-8", errors="replace")
    max_chars = int(sample.get("max_input_chars") or 0)
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
    return text


class InstrumentedClient:
    """Wrap OpenAI client to capture usage per call."""

    def __init__(
        self,
        client: OpenAI,
        tracker: UsageTracker,
        *,
        provider: str,
        model: str,
        stage: str,
        sample_id: str,
        run_index: int,
    ):
        self._client = client
        self._tracker = tracker
        self._provider = provider
        self._model = model
        self._stage = stage
        self._sample_id = sample_id
        self._run_index = run_index

    @property
    def chat(self):
        return _ChatProxy(self)


class _ChatProxy:
    def __init__(self, outer: InstrumentedClient):
        self._outer = outer
        self.completions = _CompletionsProxy(outer)


class _CompletionsProxy:
    def __init__(self, outer: InstrumentedClient):
        self._outer = outer

    def create(self, **kwargs):
        model = kwargs.get("model") or self._outer._model
        t0 = time.time()
        err = ""
        success = True
        pt = ct = cached = total = 0
        try:
            resp = self._outer._client.chat.completions.create(**kwargs)
            usage = getattr(resp, "usage", None)
            if usage:
                pt = getattr(usage, "prompt_tokens", 0) or 0
                ct = getattr(usage, "completion_tokens", 0) or 0
                total = getattr(usage, "total_tokens", 0) or (pt + ct)
                details = getattr(usage, "prompt_tokens_details", None)
                if details:
                    cached = getattr(details, "cached_tokens", 0) or 0
            return resp
        except Exception as exc:
            success = False
            err = str(exc)[:500]
            raise
        finally:
            if success:
                self._outer._tracker.add(
                    UsageRecord(
                        provider=self._outer._provider,
                        model=model,
                        stage=self._outer._stage,
                        sample_id=self._outer._sample_id,
                        run_index=self._outer._run_index,
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        cached_prompt_tokens=cached,
                        total_tokens=total,
                        latency_sec=time.time() - t0,
                        success=success,
                        error=err,
                    )
                )


def create_provider_client(provider: str, config: dict) -> OpenAI:
    if provider == "openrouter":
        return OpenAI(
            api_key=config["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
        )
    return OpenAI(api_key=config["OPENAI_API_KEY"])


def run_preprocess(
    *,
    client: OpenAI,
    model: str,
    source_text: str,
    token_query: str,
    token_input_role: str,
    retention: Tuple[int, int],
    skip_merge_reminimize: bool,
    tracker: UsageTracker,
    provider: str,
    sample_id: str,
    run_index: int,
) -> str:
    import stt_function_v3 as stt

    wrapped = InstrumentedClient(
        client, tracker,
        provider=provider, model=model, stage="preprocess",
        sample_id=sample_id, run_index=run_index,
    )
    return stt.token_minimizer_chunked(
        token_input_role,
        token_query,
        source_text,
        wrapped,  # type: ignore[arg-type]
        model=model,
        skip_merge_reminimize=skip_merge_reminimize,
    )


def run_main_summarize(
    *,
    client: OpenAI,
    model: str,
    concise_text: str,
    filename: str,
    input_prompt: str,
    token_range: Tuple[float, float],
    tracker: UsageTracker,
    provider: str,
    sample_id: str,
    run_index: int,
    fallback_client: Optional[OpenAI] = None,
    fallback_model: Optional[str] = None,
) -> str:
    import stt_function_v3 as stt

    wrapped = InstrumentedClient(
        client, tracker,
        provider=provider, model=model, stage="main",
        sample_id=sample_id, run_index=run_index,
    )
    fb_wrapped = None
    if fallback_client and fallback_model:
        fb_wrapped = InstrumentedClient(
            fallback_client, tracker,
            provider="openrouter", model=fallback_model, stage="main_fallback",
            sample_id=sample_id, run_index=run_index,
        )
    return stt.summarize_with_chunking(
        transcription=concise_text,
        filename=filename,
        prompt=input_prompt,
        client=wrapped,  # type: ignore[arg-type]
        token_range=token_range,
        language="Korean",
        style="Markdown",
        model=model,
        fallback_client=fb_wrapped,  # type: ignore[arg-type]
        fallback_model=fallback_model,
        primary_provider=provider,
        fallback_provider="openrouter" if fallback_model else None,
    )


class FailingPrimaryClient:
    """Mock client that always raises retryable API errors."""

    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                from openai import NotFoundError

                raise NotFoundError(
                    "Simulated primary failure for fallback test",
                    response=None,
                    body={"error": {"message": "model not found"}},
                )


def simulate_fallback_path(
    *,
    openai_client: OpenAI,
    fallback_client: OpenAI,
    fallback_model: str,
    concise_text: str,
    filename: str,
    input_prompt: str,
    token_range: Tuple[float, float],
    tracker: UsageTracker,
    sample_id: str,
    run_index: int,
) -> Tuple[str, bool]:
    """Inject primary failure via invalid model ID; verify fallback produces output."""
    import stt_function_v3 as stt

    fb_wrapped = InstrumentedClient(
        fallback_client, tracker,
        provider="openrouter", model=fallback_model, stage="fallback_sim",
        sample_id=sample_id, run_index=run_index,
    )
    primary_wrapped = InstrumentedClient(
        openai_client, tracker,
        provider="openai", model="openai/invalid-smoke-benchmark", stage="fallback_sim_primary",
        sample_id=sample_id, run_index=run_index,
    )
    try:
        out = stt.summarize_with_chunking(
            transcription=concise_text[:12000],
            filename=filename,
            prompt=input_prompt,
            client=primary_wrapped,  # type: ignore[arg-type]
            token_range=token_range,
            model="openai/invalid-smoke-benchmark",
            fallback_client=fb_wrapped,  # type: ignore[arg-type]
            fallback_model=fallback_model,
            primary_provider="openai",
            fallback_provider="openrouter",
        )
        return out or "", bool(out and len(out) > 100 and "##" in out)
    except Exception as exc:
        logger.warning("Fallback simulation failed: %s", exc)
        return "", False


def run_blind_judge(
    judge_client: OpenAI,
    judge_model: str,
    *,
    stage: str,
    source_excerpt: str,
    candidates: Dict[str, str],
    tracker: UsageTracker,
    sample_id: str,
) -> Dict[str, float]:
    rubric = PREPROCESS_JUDGE_RUBRIC if stage == "preprocess" else MAIN_JUDGE_RUBRIC
    prompt = build_blind_judge_prompt(stage, source_excerpt, candidates, rubric)
    wrapped = InstrumentedClient(
        judge_client, tracker,
        provider="openai", model=judge_model, stage=f"judge_{stage}",
        sample_id=sample_id, run_index=0,
    )
    resp = wrapped.chat.completions.create(
        model=judge_model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    scores: Dict[str, float] = {}
    for k, v in data.items():
        if isinstance(v, dict) and "score" in v:
            scores[k.upper()] = float(v["score"])
        elif isinstance(v, (int, float)):
            scores[k.upper()] = float(v)
    return scores
