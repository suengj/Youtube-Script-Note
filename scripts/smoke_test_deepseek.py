#!/usr/bin/env python3
"""Backward-compatible entrypoint; prefer scripts/smoke_test_main_llm.py."""

from smoke_test_main_llm import main

if __name__ == "__main__":
    raise SystemExit(main())
