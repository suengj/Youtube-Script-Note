"""A/B/C/D P03 v5 benchmark. Dry-run is fully offline."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import stt_function_v3 as stt
from llm_responses import call_responses
from scripts.md_mobile_utils import assemble_mobile_md, build_save_entry, prepare_mobile_body

PRICING = ROOT / "pricing" / "openai_pricing_2026-10-01.json"
PROMPT_PATH = ROOT / "prompt" / os.getenv("DIRECT_PROMPT_FILE", "direct_luna_v5_p2.md")
MODELS = {"A": "gpt-5-nano-2025-08-07 + gpt-5-mini-2025-08-07",
          "B": "gpt-6-luna (low)", "C": "gpt-6-luna (medium)", "D": "gpt-6-luna (high)"}


class FakeClient:
    """Deterministic offline response shape for dry-run smoke and wiring checks."""
    def __init__(self):
        self.responses = SimpleNamespace(create=self._responses_create)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat_create))

    @staticmethod
    def _text():
        return ("# Benchmark source\n\n## 한눈에 보기\n- [확정] 오프라인 benchmark 응답.\n\n"
                "## 핵심 주제\n- [확정] 원문 의미와 수치를 보존한다.\n- [정황] 실제 모델 호출은 수행하지 않았다.\n\n"
                "> [!note]- Insights\n> - [추정] 이 결과는 배선 확인용이다.\n\n"
                "> [!note]- Key Takeaways\n> - 실제 품질 평가는 live benchmark에서 확인한다.\n\n## Tags\n- benchmark\n- offline")

    def _responses_create(self, **kwargs):
        return SimpleNamespace(id="dry-response", model=kwargs["model"], status="completed",
            service_tier=None, output_text=self._text(), usage=SimpleNamespace(
                input_tokens=12, output_tokens=20, total_tokens=32,
                input_tokens_details=SimpleNamespace(cached_tokens=2),
                output_tokens_details=SimpleNamespace(reasoning_tokens=5)))

    def _chat_create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self._text()))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=20, total_tokens=32,
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=2),
                                  completion_tokens_details=SimpleNamespace(reasoning_tokens=5)))


class LegacyCaptureClient:
    def __init__(self, client, emit):
        self.client, self.emit = client, emit
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        start = time.monotonic()
        model = kwargs.get("model")
        stage = "preprocess" if "nano" in (model or "") else "summarize"
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            self.emit({"model_requested": model, "endpoint": "chat.completions", "stage": stage,
                       "input_tokens": None, "cached_tokens": None, "cache_write_tokens": None,
                       "output_tokens": None, "reasoning_tokens": None, "total_tokens": None,
                       "latency_s": round(time.monotonic() - start, 3), "attempts": 1,
                       "status": "error", "error_category": type(exc).__name__})
            raise
        u = getattr(response, "usage", None)
        self.emit({"model_requested": model, "endpoint": "chat.completions", "stage": stage,
                   "input_tokens": getattr(u, "prompt_tokens", None) if u else None,
                   "cached_tokens": getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None) if u else None,
                   "cache_write_tokens": None,
                   "output_tokens": getattr(u, "completion_tokens", None) if u else None,
                   "reasoning_tokens": getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", None) if u else None,
                   "total_tokens": getattr(u, "total_tokens", None) if u else None,
                   "latency_s": round(time.monotonic() - start, 3), "attempts": 1, "status": "completed"})
        return response


def _source_text(source_path: str) -> str:
    path = Path(source_path)
    if not path.is_absolute():
        path = ROOT / path
    if path.suffix.lower() == ".vtt":
        return stt.subtitle_file_to_plain_text(str(path))
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        return value.get("text") or value.get("transcript") or ""
    return path.read_text(encoding="utf-8", errors="replace")


def _render(output: str, out_path: Path, source: dict, suffix: str) -> str:
    body, tags, title, tldr = prepare_mobile_body(output)
    if not title:
        title = source.get("title", "")
    entry = build_save_entry(md_abs_path=str(out_path), md_root=str(out_path.parent),
        vid=source["video_id"], channel=source.get("category", ""), upload_date="",
        transcript_date="", lang=source.get("language", "ko"), suffix=suffix,
        source_url="", tags=tags, title=title, tldr=tldr)
    return assemble_mobile_md(entry, body)


def run(manifest_path: Path, out: Path, arms: list[str], dry_run: bool) -> None:
    from main import INPUT_PROMPT, TOKEN_INPUT_ROLE, build_token_query

    token_count_method = "tiktoken cl100k_base"
    original_get_encoding = stt.tiktoken.get_encoding
    if dry_run:
        # Do not trigger tiktoken's first-use vocabulary download in an offline dry run.
        class OfflineEncoding:
            def encode(self, text):
                return text.split()
        stt.tiktoken.get_encoding = lambda _name: OfflineEncoding()
        token_count_method = "offline whitespace estimate (tiktoken unavailable without network)"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sources = manifest.get("sources", [])
    if len(sources) != 3:
        raise ValueError("manifest must list exactly three sources")
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
    out.mkdir(parents=True, exist_ok=True)
    usage_path = out / "usage.jsonl"
    frozen = []
    for source in sources:
        raw = _source_text(source["source_path"])
        category = source["category"]
        frozen_path = out / "sources" / category / "source.txt"
        frozen_path.parent.mkdir(parents=True, exist_ok=True)
        if frozen_path.exists() and frozen_path.read_text(encoding="utf-8") != raw:
            raise ValueError(f"frozen source changed for category {category}")
        if not frozen_path.exists():
            frozen_path.write_text(raw, encoding="utf-8")
        frozen.append({**source, "text": raw, "frozen_path": str(frozen_path.relative_to(out)),
                       "sha256": hashlib.sha256(raw.encode()).hexdigest(), "raw_tokens": stt.count_tokens(raw)})
    client = FakeClient() if dry_run else __import__("openai").OpenAI()
    for src in frozen:
        for arm in arms:
            output_path = out / "outputs" / src["category"] / f"{arm}.md"
            if output_path.exists():
                prior = output_path.read_text(encoding="utf-8", errors="replace")
                if "format_version:" in prior and prior.lstrip().startswith("---") and "\n# " in prior:
                    continue
            output_path.parent.mkdir(parents=True, exist_ok=True)
            call_index = 0
            def emit(record, stage):
                nonlocal call_index
                call_index += 1
                row = {**record, "arm": arm, "category": src["category"], "video_id": src["video_id"],
                       "call_index": call_index, "stage": stage}
                if row.get("endpoint") == "chat.completions":
                    model = row.get("model_requested") or ""
                    rate = {"gpt-5-nano-2025-08-07": (0.05, 0.005, 0.40),
                            "gpt-5-mini-2025-08-07": (0.25, 0.025, 2.00)}.get(model)
                    if rate and row.get("input_tokens") is not None and row.get("output_tokens") is not None:
                        inp, cached, output_rate = rate
                        cached_tokens = row.get("cached_tokens") or 0
                        row["est_cost_usd"] = ((row["input_tokens"] - cached_tokens) * inp + cached_tokens * cached
                                               + row["output_tokens"] * output_rate) / 1_000_000
                    if row.get("output_tokens") is not None and row.get("reasoning_tokens") is not None:
                        row["visible_output_tokens_est"] = max(0, row["output_tokens"] - row["reasoning_tokens"])
                with usage_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if arm == "A":
                wrapped = LegacyCaptureClient(client, lambda row: emit(row, row.get("stage", "legacy")))
                ret_min, ret_max = (80, 95) if src.get("source_type") != "auto_subs" else (60, 80)
                concise = stt.token_minimizer_chunked(TOKEN_INPUT_ROLE,
                    build_token_query(ret_min, ret_max, auto_subs=src.get("source_type") == "auto_subs"),
                    src["text"], wrapped, model="gpt-5-nano-2025-08-07", skip_merge_reminimize=True)
                result = stt.summarize_with_chunking(concise, src.get("title", src["video_id"]), INPUT_PROMPT,
                    wrapped, model="gpt-5-mini-2025-08-07", token_range=(1.5, 2.0), language="Korean", style="Markdown")
            else:
                effort = {"B": "low", "C": "medium", "D": "high"}[arm]
                try:
                    result, usage = call_responses(client, "gpt-6-luna", prompt,
                        f"# {src.get('title', src['video_id'])}\n\n{src['text']}", effort=effort,
                        max_output_tokens=32000, sample_video_id=src["video_id"], source_text=src["text"])
                except Exception as exc:
                    usage = getattr(exc, "usage", None)
                    if usage:
                        for failed in usage.attempt_errors:
                            emit({"model_requested": usage.model_requested, "endpoint": "responses",
                                  "attempt_number": failed["attempt"], "attempts": usage.attempts,
                                  "status": "error", "error_category": failed["category"],
                                  "latency_s": None, "input_tokens": None, "cached_tokens": None,
                                  "output_tokens": None, "reasoning_tokens": None, "total_tokens": None}, "direct_luna")
                    raise
                for failed in usage.attempt_errors:
                    emit({"model_requested": usage.model_requested, "endpoint": "responses",
                          "attempt_number": failed["attempt"], "attempts": usage.attempts,
                          "status": "error", "error_category": failed["category"],
                          "latency_s": None, "input_tokens": None, "cached_tokens": None,
                          "output_tokens": None, "reasoning_tokens": None, "total_tokens": None}, "direct_luna")
                emit(usage.to_dict(), "direct_luna")
            suffix = "5-mini" if arm == "A" else "luna-" + {"B": "low", "C": "medium", "D": "high"}[arm]
            rendered = _render(result, output_path, src, suffix)
            output_path.write_text(rendered, encoding="utf-8")
    git_sha = "unknown"
    try:
        import subprocess
        git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        pass
    (out / "manifest.json").write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(), "code_git_sha": git_sha,
        "prompt_sha256": prompt_sha, "models": MODELS, "pricing_snapshot": json.loads(PRICING.read_text()),
        "dry_run": dry_run, "raw_token_count_method": token_count_method,
        "sources": [{k: s.get(k) for k in ("category", "video_id", "title", "language", "source_type", "frozen_path", "sha256", "raw_tokens")} for s in frozen],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_comparison(out, frozen, arms)
    if dry_run:
        stt.tiktoken.get_encoding = original_get_encoding


def _write_comparison(out: Path, sources: list[dict], arms: list[str]) -> None:
    records = [json.loads(line) for line in (out / "usage.jsonl").read_text(encoding="utf-8").splitlines()] if (out / "usage.jsonl").exists() else []
    rows = []
    for source in sources:
        for arm in arms:
            calls = [x for x in records if x["category"] == source["category"] and x["arm"] == arm]
            def sums(key):
                values = [x.get(key) for x in calls]
                if not values or any(value is None for value in values):
                    return "NOT_EXPOSED"
                return sum(values)
            def rounded_sum(key, digits):
                value = sums(key)
                return round(value, digits) if isinstance(value, (int, float)) else value
            rows.append({"category": source["category"], "arm": arm, "calls": len(calls),
                "raw_input_tokens": source["raw_tokens"], "api_input_tokens": sums("input_tokens"),
                "cached_tokens": sums("cached_tokens"), "reasoning_tokens": sums("reasoning_tokens"),
                "visible_output_tokens_est": sums("visible_output_tokens_est"), "total_tokens": sums("total_tokens"),
                "latency_s": rounded_sum("latency_s", 3), "cost_usd": rounded_sum("est_cost_usd", 8)})
    with (out / "comparison.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader(); writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--arms", default="A,B,C,D")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    arms = [x.strip().upper() for x in args.arms.split(",") if x.strip()]
    if not arms or set(arms) - set(MODELS):
        parser.error("--arms must contain A, B, C, or D")
    run(args.manifest, args.out, arms, args.dry_run)


if __name__ == "__main__":
    main()
