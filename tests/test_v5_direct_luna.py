import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import llm_responses
import main
from benchmarks.llm.v5_single_pass import run as run_benchmark


class FakeResponses:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response
        self.error = error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def response(*, status="completed", output="## 한눈에 보기\n- [확정] 보존", input_tokens=100,
             output_tokens=50, cached=10, reasoning=20, incomplete_reason=None):
    return SimpleNamespace(id="resp_1", model="gpt-6-luna-2026-09", status=status,
        service_tier="default", output_text=output,
        incomplete_details=SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=cached, cache_write_tokens=5),
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning)))


def test_direct_pipeline_makes_one_responses_request_and_passes_effort(monkeypatch):
    monkeypatch.setattr(main.stt.tiktoken, "get_encoding", lambda _: SimpleNamespace(encode=lambda text: text.split()))
    api = FakeResponses(response=response())
    client = SimpleNamespace(responses=SimpleNamespace(create=api.create))
    text, usage = main.run_direct_summary(client, "English source transcript", "Title", "vid1", {
        "DIRECT_LLM_MODEL": "gpt-6-luna", "DIRECT_LLM_REASONING_EFFORT": "high",
        "DIRECT_MAX_INPUT_TOKENS": 200000, "DIRECT_MAX_OUTPUT_TOKENS": 32000,
    })
    assert len(api.calls) == 1
    assert api.calls[0]["reasoning"] == {"effort": "high"}
    assert api.calls[0]["input"].endswith("English source transcript")
    assert "English or mixed-language prose faithfully into Korean" in api.calls[0]["instructions"]
    assert text.startswith("## 한눈에")
    assert usage.sample_video_id == "vid1"


def test_usage_parsing_cached_reasoning_and_no_double_count():
    api = FakeResponses(response=response(output_tokens=100, reasoning=80))
    result, usage = llm_responses.call_responses(SimpleNamespace(responses=SimpleNamespace(create=api.create)),
        "gpt-6-luna", "instructions", "text", effort="low")
    assert usage.cached_tokens == 10
    assert usage.reasoning_tokens == 80
    assert usage.visible_output_tokens_est == 20
    assert usage.est_cost_usd == pytest.approx((85 * .10 + 10 * .01 + 5 * .125 + 100 * .50) / 1_000_000)


def test_missing_reasoning_is_unknown_not_zero():
    resp = response()
    resp.usage.output_tokens_details = SimpleNamespace()
    api = FakeResponses(response=resp)
    _, usage = llm_responses.call_responses(SimpleNamespace(responses=SimpleNamespace(create=api.create)),
        "gpt-6-luna", "i", "t")
    assert usage.reasoning_tokens is None
    assert usage.visible_output_tokens_est is None


def test_long_context_pricing_applies_multipliers():
    cost, parts, version = llm_responses._price("gpt-6-luna", 300000, 0, 0, 100)
    assert version == "2026-10-01"
    assert parts["input_usd"] == pytest.approx(300000 * .10 * 2 / 1_000_000)
    assert parts["output_usd"] == pytest.approx(100 * .50 * 1.5 / 1_000_000)


@pytest.mark.parametrize("status,reason,category", [
    ("incomplete", "max_output_tokens", "max_output_tokens"),
    ("completed", None, "empty_output"),
])
def test_incomplete_or_empty_output_raises_without_followup(status, reason, category):
    output = "" if status == "completed" else "content"
    api = FakeResponses(response=response(status=status, output=output, incomplete_reason=reason))
    with pytest.raises(llm_responses.ResponsesCallError) as exc:
        llm_responses.call_responses(SimpleNamespace(responses=SimpleNamespace(create=api.create)),
            "gpt-6-luna", "i", "t", retries=3)
    assert exc.value.category == category
    assert len(api.calls) == 1


def test_retryable_error_retries_and_records_categories():
    class APIConnectionError(Exception):
        pass
    api = FakeResponses(response=response())
    calls = 0
    def create(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise APIConnectionError("temporary connection issue")
        return api.create(**kwargs)
    _, usage = llm_responses.call_responses(SimpleNamespace(responses=SimpleNamespace(create=create)),
        "gpt-6-luna", "i", "t", retries=1, backoff_s=0)
    assert usage.attempts == 2
    assert usage.attempt_errors == [{"attempt": 1, "category": "connection"}]


def test_huge_input_refused_without_api_call(monkeypatch):
    monkeypatch.setattr(main.stt.tiktoken, "get_encoding", lambda _: SimpleNamespace(encode=lambda _text: list(range(201))))
    api = FakeResponses(response=response())
    with pytest.raises(llm_responses.ResponsesCallError) as exc:
        main.run_direct_summary(SimpleNamespace(responses=SimpleNamespace(create=api.create)), "large", "t", "v",
            {"DIRECT_MAX_INPUT_TOKENS": 200})
    assert exc.value.category == "input_too_long"
    assert api.calls == []


def test_legacy_mode_does_not_use_direct_adapter(monkeypatch):
    called = []
    monkeypatch.setattr(main, "call_responses", lambda *a, **k: called.append(1))
    # Legacy dispatch remains MainLlmConfig.summarize -> summarize_with_chunking.
    class Legacy:
        def summarize(self, **kwargs):
            assert kwargs["transcription"] == "concise"
            return "legacy markdown"
    result, usage = main.summarize_with_pipeline("legacy_two_stage", direct_client=object(), main_llm=Legacy(),
        direct_transcription="raw", legacy_transcription="concise", filename="n", video_id="v",
        config={}, prompt="p")
    assert result == "legacy markdown"
    assert usage is None
    assert called == []


def test_app_version_and_frontmatter_contract():
    assert config.APP_VERSION == "5.0.0"
    source = Path("scripts/md_mobile_utils.py").read_text()
    assert 'fm_entry["format_version"] = "4.1"' in source


def test_benchmark_dry_run_writes_twelve_markdown_outputs(tmp_path):
    manifest = {"sources": []}
    for i, category in enumerate(("a", "b", "c")):
        path = tmp_path / f"source{i}.txt"
        path.write_text("Short synthetic transcript with a company name and 42 units.", encoding="utf-8")
        manifest["sources"].append({"category": category, "video_id": f"v{i}", "title": f"Title {i}",
            "source_path": str(path), "language": "en", "source_type": "whisper"})
    manifest_path = tmp_path / "input.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    out = tmp_path / "run"
    run_benchmark(manifest_path, out, ["A", "B", "C", "D"], True)
    assert len(list((out / "outputs").glob("*/*.md"))) == 12
    assert (out / "usage.jsonl").exists()
    assert (out / "manifest.json").exists()
    assert (out / "comparison.csv").exists()
    assert all("format_version: '4.1'" in p.read_text() for p in (out / "outputs").glob("*/*.md"))


def test_mlx_loader_does_not_recurse(monkeypatch):
    import sys, types
    import stt_function_v3 as stt_mod
    monkeypatch.setattr(stt_mod, "mlx_whisper", None)
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=lambda *a, **k: None))
    assert stt_mod._load_mlx_whisper() is sys.modules["mlx_whisper"]


def test_cache_write_tokens_not_double_billed():
    from llm_responses import _price
    total, parts, _ = _price("gpt-6-luna", 1_000_000, 0, 1_000_000, 0)
    assert parts["input_usd"] == 0
    assert abs(total - 0.125) < 1e-9


def test_primary_lang_uses_detected_original_language():
    import stt_function_v3 as stt_mod
    ac = {k: [] for k in ("ab", "en", "en-orig", "ko", "ko-orig", "ja")}
    langs = ["en", "ko", "ja"]
    # Korean video without defaultAudioLanguage: must not fall back to translated 'en'
    assert stt_mod._resolve_primary_lang({"automatic_captions": ac, "language": "ko"}, None, langs) == "ko-orig"
    # English video reported as en-US picks the original English track
    assert stt_mod._resolve_primary_lang({"automatic_captions": ac, "language": "en-US"}, None, langs) == "en-orig"
    # explicit prefer_lang still wins
    assert stt_mod._resolve_primary_lang({"automatic_captions": ac, "language": "ko"}, "ja", langs) == "ja"
    # no detected language: legacy subs_langs order unchanged
    assert stt_mod._resolve_primary_lang({"automatic_captions": ac}, None, langs) == "en"


def test_retry_script_has_direct_luna_single_call_branch():
    src = Path("scripts/retry_small_summary_auto_subs.py").read_text()
    assert 'config.get("LLM_PIPELINE_MODE") == "direct_luna"' in src
    assert "run_direct_summary(openai_client, transcription" in src


def test_direct_prompt_v31_is_default_cacheable_and_parser_compatible():
    import tiktoken
    import main as main_mod
    text = Path("prompt/direct_luna_v5_p3.md").read_text(encoding="utf-8")
    n = len(tiktoken.get_encoding("o200k_base").encode(text))
    assert 1100 <= n <= 1500  # >1,024-token static prefix so it is prompt-cached
    assert main_mod.DIRECT_PROMPT_FILE == "direct_luna_v5_p3.md"
    assert main_mod.DIRECT_LUNA_PROMPT == text
    for marker in ("## 한눈에 보기", "> [!note]- Insights", "> [!note]- Key Takeaways", "## Tags", "one `- tag` per line"):
        assert marker in text
    assert "{" not in text.replace("{video title}", "")  # no per-video dynamic content
    assert Path("prompt/direct_luna_v5.md").exists()  # V1 kept for rollback
    assert Path("prompt/direct_luna_v5_p2.md").exists()  # V3 kept for rollback
