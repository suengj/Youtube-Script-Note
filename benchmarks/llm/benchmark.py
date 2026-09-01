#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P03 LLM benchmark CLI.

Examples:
  python -m benchmarks.llm.benchmark --stage preprocess --runs 2
  python -m benchmarks.llm.benchmark --stage main --runs 2 --quick
  python -m benchmarks.llm.benchmark --stage end-to-end --runs 2
  python -m benchmarks.llm.benchmark --stage fallback
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from benchmarks.llm.cost import (  # noqa: E402
    aggregate_costs,
    cost_projection,
    estimate_cost_usd,
    pareto_dominates,
)
from benchmarks.llm.runner import (  # noqa: E402
    create_provider_client,
    load_manifest,
    load_sample_text,
    run_blind_judge,
    run_main_summarize,
    run_preprocess,
    simulate_fallback_path,
)
from benchmarks.llm.scoring import (  # noqa: E402
    score_main_deterministic,
    score_preprocess_deterministic,
)
from benchmarks.llm.cost import UsageRecord, UsageTracker  # noqa: E402
from main import (  # noqa: E402
    INPUT_PROMPT,
    MAIN_LLM_TOKEN_RANGE,
    TOKEN_INPUT_ROLE,
    build_token_query,
    load_config,
)

MANIFEST_PATH = Path(__file__).parent / "dataset_manifest.json"
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts" / "llm_benchmark"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("llm_benchmark")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_preprocess_benchmark(
    manifest: Dict[str, Any],
    config: dict,
    *,
    runs: int,
    quick: bool,
    use_judge: bool,
    out_dir: Path,
) -> List[Dict[str, Any]]:
    samples = manifest["samples"]
    if quick:
        samples = samples[:4]
    models = manifest["model_candidates"]["preprocess"]
    tracker = UsageTracker()
    results: List[Dict[str, Any]] = []
    openai_client = create_provider_client("openai", config)

    for sample in samples:
        source = load_sample_text(sample, PROJECT_ROOT)
        retention = tuple(sample.get("retention") or [80, 95])
        token_query = build_token_query(retention[0], retention[1], auto_subs=bool(sample.get("auto_subs")))
        skip_merge = bool(config.get("SKIP_MERGE_REMINIMIZE", True))

        for model_spec in models:
            for run_i in range(runs):
                t0 = time.time()
                err = ""
                output = ""
                try:
                    client = openai_client if model_spec["provider"] == "openai" else create_provider_client(model_spec["provider"], config)
                    output = run_preprocess(
                        client=client,
                        model=model_spec["model"],
                        source_text=source,
                        token_query=token_query,
                        token_input_role=TOKEN_INPUT_ROLE,
                        retention=retention,  # type: ignore[arg-type]
                        skip_merge_reminimize=skip_merge,
                        tracker=tracker,
                        provider=model_spec["provider"],
                        sample_id=sample["id"],
                        run_index=run_i,
                    )
                except Exception as exc:
                    err = str(exc)[:500]
                score = score_preprocess_deterministic(source, output, retention_target=retention)  # type: ignore[arg-type]
                row = {
                    "stage": "preprocess",
                    "sample_id": sample["id"],
                    "category": sample["category"],
                    "model_label": model_spec["label"],
                    "model": model_spec["model"],
                    "provider": model_spec["provider"],
                    "run_index": run_i,
                    "success": not err and bool(output.strip()),
                    "error": err,
                    "latency_sec": round(time.time() - t0, 3),
                    "deterministic_score": score.deterministic,
                    "score_details": score.details,
                    "flags": score.flags,
                    "output_chars": len(output),
                    "source_chars": len(source),
                }
                results.append(row)
                raw_path = out_dir / "raw" / "preprocess" / sample["id"] / f"{model_spec['label']}_run{run_i}.txt"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                if output:
                    raw_path.write_text(output, encoding="utf-8")

        if use_judge and len(models) >= 2:
            by_label: Dict[str, str] = {}
            for model_spec in models:
                paths = sorted((out_dir / "raw" / "preprocess" / sample["id"]).glob(f"{model_spec['label']}_run0.txt"))
                if paths:
                    by_label[model_spec["label"][:1].upper()] = paths[0].read_text(encoding="utf-8")
            if len(by_label) >= 2:
                judge_model = manifest.get("judge_model", "gpt-4.1-mini-2025-04-14")
                judge_scores = run_blind_judge(
                    openai_client, judge_model,
                    stage="preprocess",
                    source_excerpt=source,
                    candidates=by_label,
                    tracker=tracker,
                    sample_id=sample["id"],
                )
                for r in results:
                    if r["sample_id"] == sample["id"] and r["run_index"] == 0:
                        letter = r["model_label"][:1].upper()
                        r["judge_score"] = judge_scores.get(letter)

    results.append({"_usage": [u.to_dict() for u in tracker.records]})
    return results


def _load_or_run_preprocess(sample: Dict[str, Any], model: str, out_dir: Path, config: dict) -> str:
    cache = out_dir / "raw" / "preprocess" / sample["id"] / f"current_nano_run0.txt"
    if model == "gpt-5-nano-2025-08-07" and cache.is_file():
        return cache.read_text(encoding="utf-8")
    # fallback: run once
    source = load_sample_text(sample, PROJECT_ROOT)
    retention = tuple(sample.get("retention") or [80, 95])
    token_query = build_token_query(retention[0], retention[1], auto_subs=bool(sample.get("auto_subs")))
    tracker = UsageTracker()
    client = create_provider_client("openai", config)
    return run_preprocess(
        client=client,
        model=model,
        source_text=source,
        token_query=token_query,
        token_input_role=TOKEN_INPUT_ROLE,
        retention=retention,  # type: ignore[arg-type]
        skip_merge_reminimize=bool(config.get("SKIP_MERGE_REMINIMIZE", True)),
        tracker=tracker,
        provider="openai",
        sample_id=sample["id"],
        run_index=0,
    )


def run_main_benchmark(
    manifest: Dict[str, Any],
    config: dict,
    *,
    runs: int,
    quick: bool,
    use_judge: bool,
    out_dir: Path,
    preprocess_out: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    samples = manifest["samples"]
    if quick:
        samples = samples[:4]
    models = manifest["model_candidates"]["main"]
    tracker = UsageTracker()
    results: List[Dict[str, Any]] = []

    for sample in samples:
        concise = _load_or_run_preprocess(sample, "gpt-5-nano-2025-08-07", out_dir, config)
        if not concise.strip():
            concise = load_sample_text(sample, PROJECT_ROOT)[:8000]
        filename = sample.get("title") or sample["id"]

        for model_spec in models:
            if model_spec["provider"] == "openrouter" and not config.get("OPENROUTER_API_KEY"):
                results.append({
                    "stage": "main",
                    "sample_id": sample["id"],
                    "model_label": model_spec["label"],
                    "model": model_spec["model"],
                    "success": False,
                    "error": "NOT TESTED — OPENROUTER_API_KEY missing",
                    "deterministic_score": None,
                })
                continue
            for run_i in range(runs):
                t0 = time.time()
                err = ""
                output = ""
                try:
                    client = create_provider_client(model_spec["provider"], config)
                    output = run_main_summarize(
                        client=client,
                        model=model_spec["model"],
                        concise_text=concise,
                        filename=filename,
                        input_prompt=INPUT_PROMPT,
                        token_range=tuple(MAIN_LLM_TOKEN_RANGE),  # type: ignore[arg-type]
                        tracker=tracker,
                        provider=model_spec["provider"],
                        sample_id=sample["id"],
                        run_index=run_i,
                    )
                    if not (output or "").strip():
                        err = "empty_response"
                except Exception as exc:
                    err = str(exc)[:500]
                score = score_main_deterministic(concise, output or "")
                row = {
                    "stage": "main",
                    "sample_id": sample["id"],
                    "category": sample["category"],
                    "model_label": model_spec["label"],
                    "model": model_spec["model"],
                    "provider": model_spec["provider"],
                    "run_index": run_i,
                    "success": not err and bool((output or "").strip()),
                    "error": err,
                    "latency_sec": round(time.time() - t0, 3),
                    "deterministic_score": score.deterministic,
                    "score_details": score.details,
                    "flags": score.flags,
                    "output_chars": len(output or ""),
                }
                results.append(row)
                raw_path = out_dir / "raw" / "main" / sample["id"] / f"{model_spec['label']}_run{run_i}.md"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                if output:
                    raw_path.write_text(output, encoding="utf-8")

    results.append({"_usage": [u.to_dict() for u in tracker.records]})
    return results


def run_e2e_benchmark(
    manifest: Dict[str, Any],
    config: dict,
    *,
    runs: int,
    quick: bool,
    out_dir: Path,
) -> List[Dict[str, Any]]:
    samples = manifest["samples"]
    if quick:
        samples = samples[:4]
    combos = manifest["e2e_combinations"]
    tracker = UsageTracker()
    results: List[Dict[str, Any]] = []

    for sample in samples:
        source = load_sample_text(sample, PROJECT_ROOT)
        retention = tuple(sample.get("retention") or [80, 95])
        token_query = build_token_query(retention[0], retention[1], auto_subs=bool(sample.get("auto_subs")))
        filename = sample.get("title") or sample["id"]
        openai = create_provider_client("openai", config)

        for combo in combos:
            for run_i in range(runs):
                t0 = time.time()
                err = ""
                md_out = ""
                try:
                    concise = run_preprocess(
                        client=openai,
                        model=combo["preprocess"],
                        source_text=source,
                        token_query=token_query,
                        token_input_role=TOKEN_INPUT_ROLE,
                        retention=retention,  # type: ignore[arg-type]
                        skip_merge_reminimize=bool(config.get("SKIP_MERGE_REMINIMIZE", True)),
                        tracker=tracker,
                        provider="openai",
                        sample_id=sample["id"],
                        run_index=run_i,
                    )
                    main_provider = "openai"
                    main_client = openai
                    if combo["main"] == "deepseek/deepseek-v4-flash":
                        main_provider = "openrouter"
                        main_client = create_provider_client("openrouter", config)
                    md_out = run_main_summarize(
                        client=main_client,
                        model=combo["main"],
                        concise_text=concise,
                        filename=filename,
                        input_prompt=INPUT_PROMPT,
                        token_range=tuple(MAIN_LLM_TOKEN_RANGE),  # type: ignore[arg-type]
                        tracker=tracker,
                        provider=main_provider,
                        sample_id=sample["id"],
                        run_index=run_i,
                    )
                except Exception as exc:
                    err = str(exc)[:500]
                score = score_main_deterministic(source, md_out or "")
                results.append({
                    "stage": "end_to_end",
                    "sample_id": sample["id"],
                    "combo_label": combo["label"],
                    "preprocess_model": combo["preprocess"],
                    "main_model": combo["main"],
                    "is_current": combo.get("is_current", False),
                    "run_index": run_i,
                    "success": not err and bool((md_out or "").strip()),
                    "error": err,
                    "latency_sec": round(time.time() - t0, 3),
                    "deterministic_score": score.deterministic,
                    "score_details": score.details,
                })

    results.append({"_usage": [u.to_dict() for u in tracker.records]})
    return results


def run_fallback_benchmark(
    manifest: Dict[str, Any],
    config: dict,
    *,
    out_dir: Path,
) -> List[Dict[str, Any]]:
    if not config.get("OPENROUTER_API_KEY"):
        return [{"stage": "fallback", "success": False, "error": "NOT TESTED — OPENROUTER_API_KEY missing"}]

    sample = manifest["samples"][0]
    source = load_sample_text(sample, PROJECT_ROOT)
    retention = tuple(sample.get("retention") or [80, 95])
    token_query = build_token_query(retention[0], retention[1], auto_subs=bool(sample.get("auto_subs")))
    openai = create_provider_client("openai", config)
    tracker = UsageTracker()
    concise = run_preprocess(
        client=openai,
        model=config.get("PREPROCESS_LLM_MODEL", "gpt-5-nano-2025-08-07"),
        source_text=source[:8000],
        token_query=token_query,
        token_input_role=TOKEN_INPUT_ROLE,
        retention=retention,  # type: ignore[arg-type]
        skip_merge_reminimize=True,
        tracker=tracker,
        provider="openai",
        sample_id=sample["id"],
        run_index=0,
    )
    fb = manifest["fallback_candidate"]
    fb_client = create_provider_client(fb["fallback_provider"], config)
    t0 = time.time()
    out, ok = simulate_fallback_path(
        openai_client=openai,
        fallback_client=fb_client,
        fallback_model=fb["fallback_model"],
        concise_text=concise,
        filename=sample.get("title") or sample["id"],
        input_prompt=INPUT_PROMPT,
        token_range=tuple(MAIN_LLM_TOKEN_RANGE),  # type: ignore[arg-type]
        tracker=tracker,
        sample_id=sample["id"],
        run_index=0,
    )
    score = score_main_deterministic(concise, out) if ok else None
    return [{
        "stage": "fallback",
        "primary": fb["primary"],
        "fallback_provider": fb["fallback_provider"],
        "fallback_model": fb["fallback_model"],
        "recovery_success": ok,
        "latency_sec": round(time.time() - t0, 3),
        "quality_score": score.deterministic if score else None,
        "output_chars": len(out),
        "_usage": [u.to_dict() for u in tracker.records],
    }]


def summarize_results(all_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    usage_rows: List[Dict[str, Any]] = []
    flat: List[Dict[str, Any]] = []
    for block in all_results:
        if isinstance(block, dict) and "_usage" in block:
            usage_rows.extend(block.pop("_usage", []))
        if isinstance(block, list):
            for item in block:
                if "_usage" in item:
                    usage_rows.extend(item.pop("_usage", []))
                flat.append(item)
        elif isinstance(block, dict) and block.get("stage"):
            if "_usage" in block:
                usage_rows.extend(block.pop("_usage", []))
            flat.append(block)

    by_key: Dict[str, List[float]] = defaultdict(list)
    latencies: Dict[str, List[float]] = defaultdict(list)
    failures: Dict[str, int] = defaultdict(int)
    for r in flat:
        if r.get("deterministic_score") is None:
            failures[r.get("model_label") or r.get("combo_label") or "unknown"] += 1
            continue
        key = r.get("model_label") or r.get("combo_label") or r.get("model", "?")
        by_key[key].append(float(r["deterministic_score"]))
        latencies[key].append(float(r.get("latency_sec") or 0))
        if not r.get("success", True):
            failures[key] += 1

    usage_records = [
        UsageRecord(
            provider=u.get("provider", ""),
            model=u.get("model", ""),
            stage=u.get("stage", ""),
            sample_id=u.get("sample_id", ""),
            run_index=int(u.get("run_index", 0)),
            prompt_tokens=int(u.get("prompt_tokens", 0)),
            completion_tokens=int(u.get("completion_tokens", 0)),
            cached_prompt_tokens=int(u.get("cached_prompt_tokens", 0)),
            total_tokens=int(u.get("total_tokens", 0)),
            latency_sec=float(u.get("latency_sec", 0)),
            retry_count=int(u.get("retry_count", 0)),
            success=bool(u.get("success", True)),
            error=u.get("error", ""),
            extra_cost_usd=float(u.get("extra_cost_usd", 0)),
        )
        for u in usage_rows
    ]
    summary: Dict[str, Any] = {
        "models": {},
        "costs": aggregate_costs(usage_records) if usage_records else {"total_usd": 0, "by_model": {}},
    }

    for key, scores in by_key.items():
        lats = latencies.get(key, [0])
        summary["models"][key] = {
            "quality_mean": round(statistics.mean(scores), 2),
            "quality_stdev": round(statistics.pstdev(scores), 2) if len(scores) > 1 else 0.0,
            "latency_p50": round(_percentile(lats, 50), 2),
            "latency_p95": round(_percentile(lats, 95), 2),
            "failure_count": failures.get(key, 0),
            "n": len(scores),
        }

    if usage_rows:
        total_cost = sum(u.get("est_cost_usd", 0) for u in usage_rows)
        n_samples = max(len({r.get("sample_id") for r in flat if r.get("sample_id")}), 1)
        summary["cost_projection"] = cost_projection(total_cost, n_samples)

    # Pareto: map label -> model id for cost lookup
    label_to_model: Dict[str, str] = {}
    for r in flat:
        lbl = r.get("model_label") or r.get("combo_label")
        mid = r.get("model") or r.get("main_model")
        if lbl and mid:
            label_to_model[lbl] = mid

    pareto: List[str] = []
    keys = list(summary["models"].keys())
    for i, a in enumerate(keys):
        dominated = False
        qa = summary["models"][a]["quality_mean"]
        model_a = label_to_model.get(a, a)
        ca = summary["costs"]["by_model"].get(model_a, 0) or 0.0001
        for j, b in enumerate(keys):
            if i == j:
                continue
            qb = summary["models"][b]["quality_mean"]
            model_b = label_to_model.get(b, b)
            cb = summary["costs"]["by_model"].get(model_b, 0) or 0.0001
            if pareto_dominates(qb, cb, qa, ca):
                dominated = True
                break
        if dominated:
            pareto.append(a)
    summary["pareto_dominated"] = pareto
    return summary


def write_report(out_dir: Path, summary: Dict[str, Any], manifest: Dict[str, Any], runtime_config: dict) -> None:
    lines = [
        "# P03 LLM Benchmark Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Runtime configuration",
        "",
        f"| Stage | Provider | Model |",
        f"|-------|----------|-------|",
        f"| Preprocess | openai | `{runtime_config.get('PREPROCESS_LLM_MODEL')}` |",
        f"| Main | {runtime_config.get('MAIN_LLM_PROVIDER')} | `{runtime_config.get('MAIN_LLM_MODEL')}` |",
    ]
    if runtime_config.get("MAIN_LLM_FALLBACK_MODEL"):
        lines.append(f"| Fallback | {runtime_config.get('MAIN_LLM_FALLBACK_PROVIDER')} | `{runtime_config.get('MAIN_LLM_FALLBACK_MODEL')}` |")
    else:
        lines.append("| Fallback | — | *not configured* |")

    lines.extend(["", "## Model summary", ""])
    for label, stats in summary.get("models", {}).items():
        lines.append(f"### {label}")
        lines.append(f"- Quality mean: **{stats['quality_mean']}** (σ={stats['quality_stdev']})")
        lines.append(f"- Latency p50/p95: {stats['latency_p50']}s / {stats['latency_p95']}s")
        lines.append(f"- Failures: {stats['failure_count']}")
        dom = "yes" if label in summary.get("pareto_dominated", []) else "no"
        lines.append(f"- Pareto dominated: {dom}")
        lines.append("")

    if summary.get("cost_projection"):
        cp = summary["cost_projection"]
        lines.extend([
            "## Cost projection (benchmark sample basis)",
            "",
            f"- Per video (preprocess+main sample avg): ${cp['per_video_usd']}",
            f"- Per 10 videos: ${cp['per_10_videos_usd']}",
            f"- Per 100 videos: ${cp['per_100_videos_usd']}",
            f"- Monthly (~300 videos): ${cp['monthly_300_videos_usd']}",
            "",
        ])

    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="P03 LLM benchmark harness")
    parser.add_argument("--stage", choices=["preprocess", "main", "end-to-end", "fallback", "all"], default="all")
    parser.add_argument("--runs", type=int, default=2, help="Runs per model/sample (min 2 recommended)")
    parser.add_argument("--quick", action="store_true", help="Use first 4 samples only")
    parser.add_argument("--no-judge", action="store_true", help="Skip blind LLM judge")
    parser.add_argument("--output-dir", type=str, default="")
    args = parser.parse_args(argv)

    config = load_config()
    manifest = load_manifest(MANIFEST_PATH)
    stamp = _utc_stamp()
    out_dir = Path(args.output_dir) if args.output_dir else ARTIFACTS_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_copy = {**manifest, "runtime_config": {
        k: config.get(k) for k in (
            "PREPROCESS_LLM_MODEL", "MAIN_LLM_PROVIDER", "MAIN_LLM_MODEL",
            "MAIN_LLM_FALLBACK_PROVIDER", "MAIN_LLM_FALLBACK_MODEL", "MAIN_LLM_OUTPUT_SUFFIX",
        )
    }, "benchmark_params": {"runs": args.runs, "quick": args.quick, "stage": args.stage}}
    (out_dir / "manifest.json").write_text(json.dumps(manifest_copy, ensure_ascii=False, indent=2), encoding="utf-8")

    all_results: List[Any] = []
    use_judge = not args.no_judge

    if args.stage in ("preprocess", "all"):
        logger.info("Running preprocess benchmark...")
        all_results.append(run_preprocess_benchmark(manifest, config, runs=args.runs, quick=args.quick, use_judge=use_judge, out_dir=out_dir))

    if args.stage in ("main", "all"):
        logger.info("Running main benchmark...")
        all_results.append(run_main_benchmark(manifest, config, runs=args.runs, quick=args.quick, use_judge=use_judge, out_dir=out_dir))

    if args.stage in ("end-to-end", "all"):
        logger.info("Running end-to-end benchmark...")
        all_results.append(run_e2e_benchmark(manifest, config, runs=args.runs, quick=args.quick, out_dir=out_dir))

    if args.stage in ("fallback", "all"):
        logger.info("Running fallback simulation...")
        all_results.append(run_fallback_benchmark(manifest, config, out_dir=out_dir))

    flat_rows: List[Dict[str, Any]] = []
    for block in all_results:
        if isinstance(block, list):
            for item in block:
                if item.get("stage") and "_usage" not in str(item.get("stage", "")):
                    row = {k: v for k, v in item.items() if k != "_usage"}
                    flat_rows.append(row)

    _write_jsonl(out_dir / "results.jsonl", flat_rows)
    summary = summarize_results(all_results)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if summary.get("models"):
        with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["label", "quality_mean", "quality_stdev", "latency_p50", "latency_p95", "failures", "pareto_dominated"])
            for label, stats in summary["models"].items():
                w.writerow([
                    label, stats["quality_mean"], stats["quality_stdev"],
                    stats["latency_p50"], stats["latency_p95"], stats["failure_count"],
                    label in summary.get("pareto_dominated", []),
                ])

    write_report(out_dir, summary, manifest, config)
    logger.info("Benchmark complete → %s", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
