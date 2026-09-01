# Public Release Audit (SUE-420)

**Baseline SHA:** `0902e4e` (private `main`, 2026-09-01)  
**Repository:** `suengj/p03_speech2text` (PRIVATE at audit time)  
**Audit date:** 2026-09-01

This document is public-safe. Detailed private findings are recorded in Linear SUE-420.

## Scan evidence

| Scan | Scope | Result |
|------|-------|--------|
| gitleaks 8.30.1 | 16 commits, ~7.25 MB | 0 leaks, exit 0 |
| Deterministic credential patterns | `git grep` on `HEAD` | Placeholders only in `.env.example`; no live tokens in current tree |
| Historical operational blobs | `git rev-list --all --objects` | **BLOCKER:** `input_df.csv`, `channel_df.csv`, `failed_urls.txt` reachable in commit `1bee5ff` and `40461af` |
| Personal path patterns | `git grep` on pre-sanitization `HEAD` | **REMEDIATED:** 167 matches in private baseline; 0 in candidate tree |
| Notebook outputs | `speech-to-text-v3.0.ipynb` | **HIGH:** persisted traceback with personal filesystem paths |
| GitHub PR metadata | PR #1–#4 | **BLOCKER:** PR #4 Linear bot comment exposes Drive folder ID and private Linear issue context |
| Actions / releases | GitHub API | 0 workflow runs, 0 releases |
| Cookie guidance | `docs/COOKIES_SETUP.md` | **MEDIUM:** recommends `chmod 644`; should be `600` |

## Classification summary

| Class | Examples | Remediation |
|-------|----------|-------------|
| PUBLIC | `main.py`, tests, `scripts/drive_yt_summary/`, benchmarks | Keep |
| GENERICIZE | `.env.example`, `config.py`, utility scripts, docs | Replace personal defaults with env/config |
| PRIVATE/REMOVE | `cursor/`, operator session docs, `docs/WEEKLY_CONTENT_QUEUE.md` | Remove from public tree |
| HISTORY-PURGE | `input_df.csv`, `channel_df.csv`, `failed_urls.txt`, PR #4 metadata | Strategy B (fresh history) |

## Strategy decision (SUE-422)

**Strategy B — private archive + fresh canonical public repo**

Rationale:
- Historical operational CSV blobs remain reachable in Git objects.
- PR #4 hosted metadata cannot be certified clean without abandoning PR history.
- In-place history rewrite cannot purge GitHub PR comments/refs.

Private archive retains existing repository object and history. Certified sanitized tree publishes as fresh `main` with clean history at canonical `suengj/p03_speech2text` after owner approval.

## Remediation plan

1. Sanitize working tree (SUE-421): paths, cookies, notebook, operator docs.
2. Package for public (SUE-423): README, SECURITY.md, requirements, reuse note.
3. Fresh orphan commit on implementation branch (SUE-422).
4. Independent certification (SUE-424).
5. Human approval gate before visibility change (SUE-425).
