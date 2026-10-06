"""Ported from intelligence-library source_classify/tests/test_jev.py @ 2e02d7a (SUE-1327)."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.jev_classify.jev import (
    JevConfigError,
    JevHTTPError,
    JevInvalidResponseError,
    JevProvider,
    JevTimeoutError,
    build_state,
    rubric_hash,
)
from scripts.jev_classify.taxonomy import TOPIC_IDS

SECRET = "sk-test-SECRET-0123456789abcdef"
SCORE_TOPICS = TOPIC_IDS[:-1]


class FakeResponse:
    def __init__(self, payload):
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _answer(level_probs):
    score = sum(i * p for i, p in enumerate(level_probs))
    top = max(range(5), key=lambda i: level_probs[i])
    return {"type": "score", "score": score, "confidence": 0.9,
            "legend": {str(i): f"level text {i}" for i in range(5)},
            "probabilities": {str(i): p for i, p in enumerate(level_probs)}}


def _payload(per_topic):
    return {"model": "jev-1.13.0", "answers": {t: _answer(per_topic[t]) for t in SCORE_TOPICS},
            "usage": {"input_tokens": 1000, "output_tokens": 30}}


ZERO = [1, 0, 0, 0, 0]
FOUR = [0, 0, 0, 0, 1]


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "key.txt"
    path.write_text(SECRET + "\n", encoding="utf-8")
    return str(path)


def _provider(key_file, opener):
    return JevProvider(key_file=key_file, opener=opener)


def test_request_shape_auth_header_and_score_mapping(key_file):
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data.decode())
        seen["timeout"] = timeout
        per = {t: ZERO for t in SCORE_TOPICS}
        per["ai_tech"] = FOUR
        per["education"] = [0, 0, 0.5, 0.5, 0]
        return FakeResponse(_payload(per))

    provider = _provider(key_file, opener)
    scores, reason, details = provider.classify_detail("---\ntitle: T\ntldr: S\n---\nbody text")
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["auth"] == f"Bearer {SECRET}"
    assert seen["body"]["model"] == "jev-1.13.0"
    assert set(seen["body"]["questions"]) == set(SCORE_TOPICS)
    assert all(q["type"] == "score" and len(q["criteria"]) == 5 for q in seen["body"]["questions"].values())
    assert seen["body"]["state"] == "Title: T\n\nExcerpt:\nbody text"
    assert scores == {"ai_tech": 100, "science_technology": 0, "investment_real_estate": 0,
                      "education": 63, "humanities_society_philosophy": 0, "other_unknown": 0}
    assert reason == "ai_tech L4: level text 4"
    assert details["categories"]["education"]["probabilities"]["3"] == 0.5
    assert details["categories"]["ai_tech"]["confidence"] == 0.9
    assert provider.usage == {"requests": 1, "input_tokens": 1000, "output_tokens": 30}


def test_other_unknown_only_when_every_category_top_level_is_zero(key_file):
    provider = _provider(key_file, lambda r, timeout: FakeResponse(_payload({t: ZERO for t in SCORE_TOPICS})))
    scores, _reason = provider.classify("---\ntitle: x\n---\nhello")
    assert scores["other_unknown"] == 100 and all(scores[t] == 0 for t in SCORE_TOPICS)


def test_prompt_version_tracks_rubric_hash(key_file, monkeypatch):
    provider = _provider(key_file, lambda r, timeout: FakeResponse({}))
    assert provider.prompt_version == f"jev-rubric-{rubric_hash()}"
    from scripts.jev_classify import jev
    monkeypatch.setattr(jev, "LEVEL_TEXTS", ("a", "b", "c", "d", "e"))
    assert JevProvider(key_file=key_file).prompt_version != provider.prompt_version


def test_state_uses_long_tldr_without_body_and_bounded_body_fallback():
    from scripts.jev_classify.jev import EXCERPT_CHARS, TLDR_MAX_CHARS

    tldr = "요약" * 1_000
    summarized = build_state(f"---\ntitle: 제목\ntldr: {tldr}\n---\n" + "본문" * 10_000)
    assert summarized == f"Title: 제목\nSummary: {tldr[:TLDR_MAX_CHARS]}"
    assert "Excerpt" not in summarized and "본문" not in summarized
    assert len(summarized) == len(f"Title: 제목\nSummary: {tldr[:TLDR_MAX_CHARS]}")

    fallback_body = "가" * 4_000
    fallback = build_state(f"---\ntitle: 제목\ntldr: 짧은 요약\n---\n{fallback_body}")
    assert fallback == f"Title: 제목\n\nExcerpt:\n{fallback_body[:EXCERPT_CHARS]}"
    assert len(fallback) == len("Title: 제목\n\nExcerpt:\n") + EXCERPT_CHARS


@pytest.mark.parametrize("error,expected", [
    (TimeoutError("slow"), JevTimeoutError),
    (urllib.error.URLError(TimeoutError()), JevTimeoutError),
    (urllib.error.URLError("dns"), JevHTTPError),
    (urllib.error.HTTPError("http://x", 429, "Too Many", {}, io.BytesIO(f"echo {SECRET}".encode())), JevHTTPError),
])
def test_transport_failures_are_typed(key_file, error, expected):
    def opener(request, timeout):
        raise error
    with pytest.raises(expected) as info:
        _provider(key_file, opener).classify("---\ntitle: x\n---\nbody")
    assert SECRET not in str(info.value) and SECRET not in repr(info.value)


@pytest.mark.parametrize("payload", [b"not json", [1], {"answers": {}}, {"answers": {t: {"type": "score"} for t in SCORE_TOPICS}}])
def test_invalid_responses_are_typed(key_file, payload):
    with pytest.raises(JevInvalidResponseError):
        _provider(key_file, lambda r, timeout: FakeResponse(payload)).classify("---\ntitle: x\n---\nbody")


def test_missing_or_empty_key_file_is_a_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    with pytest.raises(JevConfigError):
        JevProvider()
    empty = tmp_path / "empty.txt"
    empty.write_text("\n")
    with pytest.raises(JevConfigError):
        JevProvider(key_file=str(empty))
    with pytest.raises(JevConfigError) as info:
        JevProvider(key_file=str(tmp_path / "missing.txt"))
    assert "missing.txt" not in str(info.value)


def test_base_url_from_env(key_file, monkeypatch):
    monkeypatch.setenv("JEV_BASE_URL", "https://example.test/")
    monkeypatch.setenv("JEV_ALLOWED_HOSTS", "example.test")
    assert JevProvider(key_file=key_file).base_url == "https://example.test"


@pytest.mark.parametrize("base_url", ["http://api.typesafe.ai", "https://foreign.test"])
def test_disallowed_base_url_is_rejected_before_key_file_read(tmp_path, monkeypatch, base_url):
    import builtins

    monkeypatch.delenv("JEV_ALLOWED_HOSTS", raising=False)

    def unexpected_open(*args, **kwargs):
        raise AssertionError("key file must not be read for a rejected base URL")

    monkeypatch.setattr(builtins, "open", unexpected_open)
    with pytest.raises(JevConfigError, match="allowed host"):
        JevProvider(key_file=str(tmp_path / "secret-key"), base_url=base_url)




def test_rubric_hash_is_pinned_to_the_production_index():
    # The live classification-index.json (1,558 entries) was produced with this prompt_version.
    assert rubric_hash() == "b03a101ddb34"
    assert jev_model() == "jev-1.13.0"


def jev_model():
    from scripts.jev_classify.jev import JEV_MODEL
    return JEV_MODEL


def test_cache_key_matches_intelligence_library_golden_vector():
    # Golden value from intelligence-library source_classify.cli._cache_key @ 2e02d7a; a change here
    # would turn every migrated entry into a cache miss (1,542 JEV calls).
    from scripts.jev_classify.index import cache_key

    assert cache_key("a" * 64, "1.0.0", "jev", "jev-1.13.0", "jev-rubric-b03a101ddb34") == (
        "7b1af71c576b80861138ab1c9faffc400337e6ec433a6c5c8436aabaf1814c01"
    )
