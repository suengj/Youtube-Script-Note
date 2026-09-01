# -*- coding: utf-8 -*-
"""Deterministic + optional blind-judge scoring for P03 LLM benchmark."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

FILLER_KO = re.compile(r"\b(음|어|그+|네+|막|좀|아+|음+)\b")
NUMBERS = re.compile(r"\d[\d,\.]*")
CAP_WORDS = re.compile(r"\b[A-Z][a-zA-Z0-9\-]{2,}\b")
META_PATTERNS = re.compile(
    r"(as an ai|i cannot|here is the revised|commentary|acknowledgment|"
    r"물론입니다|다음은|요약하면|제가|I'll help|Sure,)",
    re.I,
)
REQUIRED_MAIN_SECTIONS = [
    "## 한눈에 보기",
    "## Tags",
]
OPTIONAL_MAIN_SECTIONS = [
    "> [!note]- Insights",
    "> [!note]- Key Takeaways",
]


@dataclass
class ScoreBreakdown:
    deterministic: float
    judge: Optional[float] = None
    total: float = 0.0
    details: Dict[str, float] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)

    def finalize(self, *, judge_weight: float = 0.0) -> "ScoreBreakdown":
        if self.judge is not None and judge_weight > 0:
            self.total = (1 - judge_weight) * self.deterministic + judge_weight * self.judge
        else:
            self.total = self.deterministic
        return self


def _ratio(a: int, b: int) -> float:
    if b <= 0:
        return 1.0 if a == 0 else 0.0
    return min(1.0, a / b)


def extract_numbers(text: str) -> List[str]:
    return NUMBERS.findall(text or "")


def extract_cap_entities(text: str) -> List[str]:
    return CAP_WORDS.findall(text or "")


def score_preprocess_deterministic(source: str, output: str, *, retention_target: Tuple[int, int] = (60, 95)) -> ScoreBreakdown:
    """Weighted deterministic preprocess score (0-100)."""
    src = source or ""
    out = output or ""
    details: Dict[str, float] = {}
    flags: List[str] = []

    if not out.strip():
        flags.append("empty_output")
        return ScoreBreakdown(deterministic=0.0, details=details, flags=flags).finalize()

    # meaning preservation via length retention in target band (30)
    src_len = max(len(src), 1)
    out_len = len(out)
    retention_pct = 100.0 * out_len / src_len
    lo, hi = retention_target
    if lo <= retention_pct <= hi:
        details["meaning_preservation"] = 30.0
    elif retention_pct < lo:
        details["meaning_preservation"] = max(0.0, 30.0 * (retention_pct / lo))
        flags.append("over_compressed")
    else:
        details["meaning_preservation"] = max(0.0, 30.0 * (hi / retention_pct))
        flags.append("under_compressed")

    # noise removal — filler reduction (20)
    src_fillers = len(FILLER_KO.findall(src))
    out_fillers = len(FILLER_KO.findall(out))
    if src_fillers == 0:
        details["noise_removal"] = 20.0
    else:
        reduction = 1.0 - _ratio(out_fillers, src_fillers)
        details["noise_removal"] = round(20.0 * max(0.0, reduction), 2)

    # hallucination guard (20)
    details["hallucination_guard"] = 0.0 if META_PATTERNS.search(out) else 20.0
    if META_PATTERNS.search(out):
        flags.append("meta_commentary")

    # numbers / proper nouns (15)
    num_score = _ratio(len(extract_numbers(out)), len(extract_numbers(src)))
    ent_score = _ratio(len(extract_cap_entities(out)), len(extract_cap_entities(src)))
    details["numbers_entities"] = round(15.0 * (0.6 * num_score + 0.4 * ent_score), 2)

    # sentence restoration proxy (10)
    src_sents = max(len(re.split(r"[.!?]\s+", src)), 1)
    out_sents = len(re.split(r"[.!?]\s+", out))
    sent_ratio = out_sents / src_sents
    if 0.5 <= sent_ratio <= 1.2:
        details["sentence_restoration"] = 10.0
    else:
        details["sentence_restoration"] = round(10.0 * max(0.0, 1.0 - abs(sent_ratio - 0.85)), 2)

    # format stability (5)
    bad_fmt = "```" in out or out.lstrip().startswith("#")
    details["format_stability"] = 0.0 if bad_fmt else 5.0
    if bad_fmt:
        flags.append("bad_format")

    det = round(sum(details.values()), 2)
    return ScoreBreakdown(deterministic=det, details=details, flags=flags).finalize()


def score_main_deterministic(concise_source: str, output: str) -> ScoreBreakdown:
    """Weighted deterministic main/report score (0-100)."""
    src = concise_source or ""
    out = output or ""
    details: Dict[str, float] = {}
    flags: List[str] = []

    if not out.strip():
        flags.append("empty_output")
        return ScoreBreakdown(deterministic=0.0, details=details, flags=flags).finalize()

    # coverage — entity/number overlap, capped (25)
    num_score = _ratio(len(set(extract_numbers(out))), max(len(set(extract_numbers(src))), 1))
    ent_score = _ratio(len(set(extract_cap_entities(out))), max(len(set(extract_cap_entities(src))), 1))
    h2_count = len(re.findall(r"^## ", out, re.M))
    coverage = min(1.0, 0.5 * (0.5 * num_score + 0.5 * ent_score) + 0.5 * min(h2_count / 4.0, 1.0))
    details["coverage"] = round(25.0 * coverage, 2)

    # factual — numbers preserved (20)
    details["factual_accuracy"] = round(20.0 * num_score, 2)

    # markdown structure (15)
    struct_pts = 0.0
    for sec in REQUIRED_MAIN_SECTIONS:
        struct_pts += 5.0 if sec in out else 0.0
        if sec not in out:
            flags.append(f"missing:{sec}")
    if out.lstrip().startswith("#"):
        struct_pts = min(15.0, struct_pts + 2.0)
    details["markdown_structure"] = struct_pts

    # omission prevention — keyword overlap (15)
    src_words = set(re.findall(r"[A-Za-z\uac00-\ud7a3]{4,}", src.lower()))
    out_words = set(re.findall(r"[A-Za-z\uac00-\ud7a3]{4,}", out.lower()))
    overlap = _ratio(len(src_words & out_words), max(len(src_words), 1))
    details["omission_prevention"] = round(15.0 * overlap, 2)

    # hallucination guard (10)
    details["hallucination_guard"] = 0.0 if META_PATTERNS.search(out) else 10.0

    # readability — not too verbose (10)
    out_tokens = len(out.split())
    src_tokens = max(len(src.split()), 1)
    ratio = out_tokens / src_tokens
    if 1.0 <= ratio <= 3.0:
        details["readability"] = 10.0
    elif ratio < 1.0:
        details["readability"] = round(10.0 * ratio, 2)
        flags.append("too_short")
    else:
        details["readability"] = round(max(0.0, 10.0 - (ratio - 3.0)), 2)
        flags.append("verbose")

    # instruction adherence (5)
    inst = 0.0
    if all(line.startswith(">") or line.strip() == "" or line.startswith("> [!")
           for block in re.findall(r"> \[!note\][^\n]*\n(?:>.*\n)+", out)
           for line in block.splitlines()[1:]
           if line.strip()):
        inst += 2.0
    if re.search(r"^## Tags\b", out, re.M):
        inst += 2.0
    if "[확정]" in out or "[정황]" in out:
        inst += 1.0
    details["instruction_adherence"] = min(5.0, inst)

    det = round(sum(details.values()), 2)
    return ScoreBreakdown(deterministic=det, details=details, flags=flags).finalize()


def build_blind_judge_prompt(
    stage: str,
    source_excerpt: str,
    candidates: Dict[str, str],
    rubric: str,
) -> str:
    blocks = []
    for label in sorted(candidates.keys()):
        blocks.append(f"=== {label} ===\n{candidates[label][:12000]}")
    joined = "\n\n".join(blocks)
    return f"""You are an impartial evaluator for a YouTube transcript pipeline ({stage} stage).
Score each candidate 0-100 using this rubric (do not favor length or verbosity):

{rubric}

Source excerpt (for reference):
{source_excerpt[:6000]}

Candidates (blind labels — model identity unknown):
{joined}

Respond ONLY with JSON like:
{{"A": {{"score": 82, "notes": "..."}}, "B": {{"score": 75, "notes": "..."}}}}
"""


PREPROCESS_JUDGE_RUBRIC = """- meaning preservation (30)
- noise removal without over-deletion (20)
- no hallucination/meta commentary (20)
- numbers and proper nouns kept (15)
- readable sentences (10)
- plain text format (5)"""

MAIN_JUDGE_RUBRIC = """- core content coverage (25)
- factual accuracy vs source (20)
- markdown structure quality (15)
- no major omissions (15)
- no hallucination (10)
- readability (10)
- instruction adherence (5)"""
