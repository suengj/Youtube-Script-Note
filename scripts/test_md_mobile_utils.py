#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for md_mobile_utils callout normalization and extraction."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.md_mobile_utils import (  # noqa: E402
    audit_callout_prefixes,
    extract_callout,
    normalize_obsidian_callouts,
)

PILOT_DIR = PROJECT_ROOT / "experiments" / "mobile_md_pilot"


def test_normalize_callout_body_prefix():
    broken = """> [!note]- Insights
- [외부지식] line one
- line two

> [!note]- Key Takeaways
- takeaway one
"""
    fixed = normalize_obsidian_callouts(broken)
    assert audit_callout_prefixes(fixed) == []
    assert "> - [외부지식] line one" in fixed
    assert "> - takeaway one" in fixed


def test_unchanged_when_already_ok():
    ok = """> [!note]- Insights
> - [외부지식] already ok
"""
    assert normalize_obsidian_callouts(ok) == ok


def test_extract_callout_insights_and_takeaways():
    body = """# Title

## 한눈에 보기
- [확정] fact

> [!note]- Insights
> - [외부지식] mid-income trap pattern
> [추정] policy continuity matters

> [!note]- Key Takeaways
> - Watch USMCA renegotiation risk
> - Prefer portfolio investment over FDI when stability rises

## Tags
- mexico
"""
    insights = extract_callout(body, "Insights")
    takeaways = extract_callout(body, "Key Takeaways")
    assert "[외부지식] mid-income trap pattern" in insights
    assert "[추정] policy continuity matters" in insights
    assert "Watch USMCA renegotiation risk" in takeaways
    assert "Prefer portfolio investment" in takeaways
    assert extract_callout(body, "Missing") == ""


def test_extract_callout_from_pilot_fixtures():
    mexico = (PILOT_DIR / "dr5z2WvEXBI_v2.md").read_text(encoding="utf-8")
    bio = (PILOT_DIR / "rXcYPHfyyRQ_v2.md").read_text(encoding="utf-8")

    m_insights = extract_callout(mexico, "Insights")
    m_takeaways = extract_callout(mexico, "Key Takeaways")
    assert len(m_insights.splitlines()) >= 2
    assert "[외부지식]" in m_insights or "[추정]" in m_insights
    assert len(m_takeaways.splitlines()) >= 3

    b_insights = extract_callout(bio, "Insights")
    b_takeaways = extract_callout(bio, "Key Takeaways")
    assert len(b_insights.splitlines()) >= 2
    assert "[외부지식]" in b_insights or "[추정]" in b_insights
    assert len(b_takeaways.splitlines()) >= 3


if __name__ == "__main__":
    test_normalize_callout_body_prefix()
    test_unchanged_when_already_ok()
    test_extract_callout_insights_and_takeaways()
    test_extract_callout_from_pilot_fixtures()
    print("ok")
