# UPDATE_LOG — callout structure fix + launchd TCC (2026-07-09)

> **Version:** `4.1.3`

## Problems

1. **Callouts:** `_5-mini` MD had `> [!note]- Insights` headers but body bullets without `>` — Obsidian callouts would not fold.
2. **Launchd outage (Jul 8–9):** Scheduled runs failed with `Operation not permitted` on `~/Documents/...` (TCC). No new MD since Jul 7 15:19.

## Changes

| Area | Change |
|------|--------|
| `scripts/md_mobile_utils.py` | `normalize_obsidian_callouts()` + wired in `prepare_mobile_body()` |
| `main.py` | INPUT_PROMPT v3.1 callout example; `load_dotenv`/`logs` from `__file__` dir |
| `stt_function_v3.py` | `MERGE_SUMMARY_PROMPT` callout prefix rule |
| `scripts/backfill_callout_prefix.py` | In-place backfill (no LLM) |
| `Code/launchd/` | Wrapper log → `~/Library/Logs/p03-speech2text/`; plist cwd → Application Support; `exec python $P03_HOME/main.py` |

## Validation

```bash
python -m py_compile main.py stt_function_v3.py scripts/md_mobile_utils.py scripts/backfill_callout_prefix.py
python scripts/test_md_mobile_utils.py
python scripts/backfill_callout_prefix.py --folder 2026_07_06 --dry-run
python scripts/backfill_callout_prefix.py --folder 2026_07_06
```

Launchd: reload plist, `launchctl kickstart gui/$(id -u)/com.user.p03-speech2text`, check `~/Library/Logs/p03-speech2text/wrapper.log`.
