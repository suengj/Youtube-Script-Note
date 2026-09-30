"""Small Responses API adapter with conservative usage and cost accounting."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import stt_function_v3 as stt

ROOT = Path(__file__).resolve().parent
PRICING_PATH = ROOT / "pricing" / "openai_pricing_2026-10-01.json"


@dataclass
class UsageRecord:
    model_requested: str
    model_returned: Optional[str]
    effort: str
    endpoint: str = "responses"
    service_tier: Optional[str] = None
    response_id: Optional[str] = None
    input_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    visible_output_tokens_est: Optional[int] = None
    total_tokens: Optional[int] = None
    latency_s: Optional[float] = None
    attempts: int = 0
    status: str = "unknown"
    incomplete_reason: Optional[str] = None
    source_sha256: Optional[str] = None
    sample_video_id: Optional[str] = None
    est_cost_usd: Optional[float] = None
    cost_components_usd: dict = field(default_factory=dict)
    pricing_version: Optional[str] = None
    attempt_errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class ResponsesCallError(RuntimeError):
    def __init__(self, category: str, message: str, usage: UsageRecord):
        super().__init__(message)
        self.category = category
        self.usage = usage


def _get(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _error_category(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if "ratelimit" in name:
        return "rate_limit"
    if "timeout" in name:
        return "timeout"
    if "connection" in name:
        return "connection"
    if "api" in name or getattr(exc, "status_code", None):
        return "api_error"
    return "non_retryable_error"


def _price(model: str, input_tokens: Optional[int], cached: Optional[int], cache_write: Optional[int], output: Optional[int]):
    if any(v is None for v in (input_tokens, output, cached)):
        return None, {}, None
    try:
        manifest = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
        model_data = manifest["models"][model]
    except (OSError, KeyError, ValueError):
        return None, {}, None
    long = input_tokens > int(manifest["long_context_boundary_tokens"])
    rates = model_data["long_context" if long else "short_context"]
    scale_in = 2 if long and model == "gpt-6-luna" else 1
    scale_out = 1.5 if long and model == "gpt-6-luna" else 1
    # cached/cache-write tokens are subsets of input_tokens; bill each input token once.
    in_count = max(0, input_tokens - (cached or 0) - (cache_write or 0))
    if "cache_write" in rates and cache_write is None:
        return None, {"input_usd": None, "cached_input_usd": None, "cache_write_usd": None, "output_usd": None}, manifest.get("version")
    parts = {
        "input_usd": in_count * rates["input"] * scale_in / 1_000_000,
        "cached_input_usd": (cached or 0) * rates["cached_input"] * scale_in / 1_000_000,
        "cache_write_usd": cache_write * rates["cache_write"] / 1_000_000 if "cache_write" in rates else None,
        "output_usd": output * rates["output"] * scale_out / 1_000_000,
    }
    return sum(v for v in parts.values() if v is not None), parts, manifest.get("version")


def call_responses(client, model: str, instructions: str, input_text: str, effort: str = "medium",
                   max_output_tokens: int = 32000, retries: int = 2, backoff_s: float = 0.25,
                   sample_video_id: Optional[str] = None, source_text: Optional[str] = None):
    """Return (visible output, UsageRecord); retry only errors accepted by the legacy classifier."""
    if effort not in {"low", "medium", "high"}:
        raise ValueError("effort must be low, medium, or high")
    source_sha = hashlib.sha256((source_text if source_text is not None else input_text).encode("utf-8")).hexdigest()
    rec = UsageRecord(model_requested=model, model_returned=None, effort=effort,
                      source_sha256=source_sha, sample_video_id=sample_video_id)
    started = time.monotonic()
    max_attempts = max(1, int(retries) + 1)
    for attempt in range(1, max_attempts + 1):
        rec.attempts = attempt
        try:
            response = client.responses.create(
                model=model, instructions=instructions, input=input_text,
                reasoning={"effort": effort}, max_output_tokens=max_output_tokens,
            )
        except Exception as exc:
            rec.attempt_errors.append({"attempt": attempt, "category": _error_category(exc)})
            if attempt >= max_attempts or not stt._is_retryable_llm_error(exc):
                rec.status = "error"
                rec.latency_s = round(time.monotonic() - started, 3)
                raise ResponsesCallError(_error_category(exc), str(exc), rec) from exc
            time.sleep(backoff_s * (2 ** (attempt - 1)))
            continue
        rec.latency_s = round(time.monotonic() - started, 3)
        rec.model_returned = _get(response, "model")
        rec.response_id = _get(response, "id")
        rec.service_tier = _get(response, "service_tier")
        rec.status = _get(response, "status", "unknown")
        details = _get(response, "usage")
        if details is not None:
            rec.input_tokens = _get(details, "input_tokens")
            rec.output_tokens = _get(details, "output_tokens")
            rec.total_tokens = _get(details, "total_tokens")
            idetails = _get(details, "input_tokens_details")
            odetails = _get(details, "output_tokens_details")
            rec.cached_tokens = _get(idetails, "cached_tokens") if idetails is not None else None
            rec.cache_write_tokens = _get(idetails, "cache_write_tokens") if idetails is not None else None
            rec.reasoning_tokens = _get(odetails, "reasoning_tokens") if odetails is not None else None
            if rec.output_tokens is not None and rec.reasoning_tokens is not None:
                rec.visible_output_tokens_est = max(0, rec.output_tokens - rec.reasoning_tokens)
        rec.est_cost_usd, rec.cost_components_usd, rec.pricing_version = _price(
            model, rec.input_tokens, rec.cached_tokens,
            rec.cache_write_tokens, rec.output_tokens)
        incomplete = _get(response, "incomplete_details")
        if rec.status == "incomplete":
            rec.incomplete_reason = _get(incomplete, "reason") if incomplete is not None else None
            category = "max_output_tokens" if rec.incomplete_reason == "max_output_tokens" else "incomplete_response"
            raise ResponsesCallError(category, f"Responses output incomplete: {rec.incomplete_reason or 'unknown'}", rec)
        output = _get(response, "output_text")
        if not output:
            chunks = []
            for item in (_get(response, "output", []) or []):
                for content in (_get(item, "content", []) or []):
                    if _get(content, "type") == "output_text":
                        chunks.append(_get(content, "text", ""))
            output = "".join(chunks)
        if not isinstance(output, str) or not output.strip():
            rec.status = "empty_output"
            raise ResponsesCallError("empty_output", "Responses API returned no visible text", rec)
        return output.strip(), rec
    raise AssertionError("unreachable")
