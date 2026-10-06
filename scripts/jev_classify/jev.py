"""JEV (TypeSafe System One) provider for YouTube source classification.

Canonical owner: suengj/Youtube-Script-Note (SUE-1327). Ported from
intelligence-library source_classify/jev.py @ 2e02d7a (SUE-1296) without semantic change:
the rubric, model pin and score mapping must stay byte-identical so that
prompt_version = jev-rubric-<hash> keeps existing index entries cache-valid.

One POST /v1/systemone per document with one anchored Score question per AES topic.
The API key is read from the file named by JEV_API_KEY_FILE and is only ever placed in the
Authorization header; it is never logged, stored, or included in an error message.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from .taxonomy import TOPIC_IDS

JEV_MODEL = "jev-1.13.0"
DEFAULT_BASE_URL = "https://api.typesafe.ai"
KEY_FILE_ENV = "JEV_API_KEY_FILE"
BASE_URL_ENV = "JEV_BASE_URL"
ALLOWED_HOSTS_ENV = "JEV_ALLOWED_HOSTS"
SCORE_TOPICS = TOPIC_IDS[:-1]
LEVEL_COUNT = 5
TLDR_MIN_CHARS = 200
TLDR_MAX_CHARS = 1500
EXCERPT_CHARS = 2500  # bounded fallback body excerpt
STATE_FORMAT = 2
DEFAULT_TIMEOUT = 60.0
JEV_SCORE_SEMANTICS = (
    "uncalibrated_relevance: ordinal JEV expected-level score rescaled to 0-100 "
    "(half-up level/4*100); not a probability and not comparable across categories or rubrics."
)

# Rubric for owner tuning (docs/JEV_CLASSIFICATION.md). Any change alters prompt_version.
LEVEL_TEXTS = (
    "Not about this topic; no meaningful mention.",
    "Passing mention only; the topic is not discussed in substance.",
    "Secondary theme; part of the document covers it, but the main subject is something else.",
    "Major theme; a substantial part of the document is about this topic.",
    "Central subject; the document is mainly about this topic.",
)
# Stricter anchors for the two broad categories (SUE-1296): a mention, background context or an indirect link
# must stay at level 0-1; levels 3-4 need the topic itself to be analysed substantively.
BROAD_LEVEL_TEXTS = (
    "Not about this topic; no meaningful mention.",
    "Mentioned, used as background or context, or only indirectly related; the topic itself is not analysed.",
    "Secondary theme; one distinct part of the document analyses this topic substantively, but the main subject is something else.",
    "Major theme; this topic is analysed substantively in a large part of the document, alongside or just behind the main subject.",
    "Central subject; the document's main subject is this topic itself and it is analysed substantively throughout.",
)
TOPIC_LEVEL_TEXTS = {"science_technology": BROAD_LEVEL_TEXTS, "humanities_society_philosophy": BROAD_LEVEL_TEXTS}
TOPIC_DEFINITIONS = {
    "ai_tech": "artificial intelligence: machine learning, LLMs, AI agents, AI products, AI tooling and AI industry",
    "science_technology": "science and technology: physics, biology, chemistry, engineering, robotics, energy, space and non-AI technology",
    "investment_real_estate": "investment and real estate: stocks, bonds, funds, portfolios, interest rates, markets, property and personal finance",
    "education": "education: teaching, learning methods, schools, students, curriculum and skill development",
    "humanities_society_philosophy": "humanities, society and philosophy: history, culture, politics, ethics, philosophy, social issues and human behavior",
}


def build_questions() -> dict[str, dict[str, Any]]:
    return {
        topic: {
            "type": "score",
            "instructions": f"How central is the topic of {TOPIC_DEFINITIONS[topic]} to the document in `state`?",
            "criteria": list(TOPIC_LEVEL_TEXTS.get(topic, LEVEL_TEXTS)),
        }
        for topic in SCORE_TOPICS
    }


def rubric_hash() -> str:
    payload = {"questions": build_questions(), "tldr_min_chars": TLDR_MIN_CHARS,
               "tldr_max_chars": TLDR_MAX_CHARS, "excerpt_chars": EXCERPT_CHARS,
               "state_format": STATE_FORMAT, "model": JEV_MODEL}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


class JevError(RuntimeError):
    """Base class for typed, per-entry JEV failures. Messages never contain the API key."""


class JevConfigError(JevError):
    pass


class JevTimeoutError(JevError):
    pass


class JevHTTPError(JevError):
    pass


class JevInvalidResponseError(JevError):
    pass


def build_state(text: str) -> str:
    """Use a sufficiently detailed front-matter summary, or a bounded body excerpt."""
    title = tldr = ""
    body = text
    if text.startswith("---"):
        match = re.search(r"^---[ \t]*$", text[3:], re.MULTILINE)
        if match:
            front = text[3:3 + match.start()]
            body = text[3 + match.end():]
            t = re.search(r"^title:[ \t]*(.*(?:\n[ \t]+.*)*)", front, re.MULTILINE)
            s = re.search(r"^tldr:[ \t]*(.*(?:\n[ \t]+.*)*)", front, re.MULTILINE)
            title = " ".join((t.group(1) if t else "").split()).strip("\"'")
            tldr = " ".join((s.group(1) if s else "").split()).strip("\"'")
    if len(tldr) >= TLDR_MIN_CHARS:
        return f"Title: {title}\nSummary: {tldr[:TLDR_MAX_CHARS]}"
    return f"Title: {title}\n\nExcerpt:\n{body.strip()[:EXCERPT_CHARS]}"


class JevProvider:
    name = "jev"
    model = JEV_MODEL
    score_semantics = JEV_SCORE_SEMANTICS

    def __init__(self, *, key_file: str | None = None, base_url: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT, opener: Any = None):
        self.prompt_version = f"jev-rubric-{rubric_hash()}"
        self.base_url = (base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
        try:
            parsed = urlsplit(self.base_url)
        except ValueError:
            raise JevConfigError(f"{BASE_URL_ENV} must use HTTPS with an allowed host") from None
        allowed_hosts = {"api.typesafe.ai"}
        allowed_hosts.update(host.strip().lower() for host in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",") if host.strip())
        if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.hostname.lower() not in allowed_hosts
                or parsed.username or parsed.password):
            raise JevConfigError(f"{BASE_URL_ENV} must use HTTPS with an allowed host")
        self.timeout = timeout
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
        self._opener = opener or urllib.request.urlopen
        path = key_file or os.environ.get(KEY_FILE_ENV)
        if not path:
            raise JevConfigError(f"{KEY_FILE_ENV} is not set")
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as stream:
                self._key = stream.read().strip()
        except OSError as exc:
            raise JevConfigError(f"cannot read key file named by {KEY_FILE_ENV}") from None
        if not self._key:
            raise JevConfigError("JEV key file is empty")

    def __repr__(self) -> str:
        return f"JevProvider(model={self.model!r}, base_url={self.base_url!r})"

    def _scrub(self, value: str) -> str:
        return value.replace(self._key, "[redacted]")

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/v1/systemone", data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                     "User-Agent": "youtube-script-note-jev-classify/1"})
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise JevHTTPError(f"JEV HTTP status {exc.code}") from None
        except TimeoutError:
            raise JevTimeoutError("JEV request timed out") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise JevTimeoutError("JEV request timed out") from None
            raise JevHTTPError("JEV connection failed") from None
        except OSError:
            raise JevHTTPError("JEV connection failed") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise JevInvalidResponseError("JEV response is not valid JSON") from None
        if not isinstance(payload, dict):
            raise JevInvalidResponseError("JEV response is not a JSON object")
        return payload

    def classify_detail(self, text: str) -> tuple[dict[str, int], str, dict[str, Any]]:
        payload = self._post({"state": build_state(text), "model": self.model, "questions": build_questions()})
        answers = payload.get("answers")
        if not isinstance(answers, dict) or set(answers) != set(SCORE_TOPICS):
            raise JevInvalidResponseError("JEV answers do not match the requested topics")
        scores: dict[str, int] = {}
        categories: dict[str, Any] = {}
        top_levels: dict[str, int] = {}
        for topic in SCORE_TOPICS:
            answer = answers[topic]
            try:
                level = float(answer["score"])
                confidence = float(answer["confidence"])
                probs = {str(i): float(answer["probabilities"][str(i)]) for i in range(LEVEL_COUNT)}
                legend = answer["legend"]
                if answer.get("type") != "score" or not 0.0 <= level <= LEVEL_COUNT - 1 or not 0.0 <= confidence <= 1.0:
                    raise ValueError
                if any(not 0.0 <= p <= 1.0 for p in probs.values()):
                    raise ValueError
                top_levels[topic] = max(range(LEVEL_COUNT), key=lambda i: (probs[str(i)], -i))
                level_text = str(legend[str(top_levels[topic])])
            except (KeyError, TypeError, ValueError):
                raise JevInvalidResponseError(f"JEV answer for {topic} is malformed") from None
            scores[topic] = int(level / (LEVEL_COUNT - 1) * 100 + 0.5)  # half-up, deterministic
            categories[topic] = {"level": round(level, 4), "confidence": round(confidence, 4),
                                 "probabilities": {k: round(v, 4) for k, v in probs.items()},
                                 "top_level": top_levels[topic], "top_level_text": level_text}
        # other_unknown: 100 only when the most probable level of every category is level 0.
        scores["other_unknown"] = 100 if all(level == 0 for level in top_levels.values()) else 0
        best = max(SCORE_TOPICS, key=lambda t: (scores[t], -SCORE_TOPICS.index(t)))
        reason = f"{best} L{top_levels[best]}: {categories[best]['top_level_text']}"
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        in_tok, out_tok = usage.get("input_tokens"), usage.get("output_tokens")
        in_tok = in_tok if isinstance(in_tok, int) else 0
        out_tok = out_tok if isinstance(out_tok, int) else 0
        self.usage["requests"] += 1
        self.usage["input_tokens"] += in_tok
        self.usage["output_tokens"] += out_tok
        details = {"model_reported": str(payload.get("model", ""))[:64], "categories": categories,
                   "usage": {"input_tokens": in_tok, "output_tokens": out_tok}}
        return scores, self._scrub(reason)[:240], details

    def classify(self, text: str) -> tuple[dict[str, int], str]:
        scores, reason, _details = self.classify_detail(text)
        return scores, reason
