# UPDATE_LOG v4 — Mobile Obsidian MD

> **현재 버전:** `4.2.0` · Phase 0 ~ 1c + v4.2 concurrency **구현 완료** (Phase 2+ 보류)

## v4.2.0 — 2-worker concurrency, VTT temp, single writer (2026-08-05)

| Area | Change |
|------|--------|
| `main.py` | Up to 2 video workers (`VIDEO_WORKERS`, clamped max 2); main-thread single writer for CSV/JSONL/catalog |
| `job_workspace.py` | Per-video `tmp/jobs/<video_id>/` (subs, wav, metadata); cleanup on success |
| `transcript_cache.py` | 72h plain transcript cache when durable full absent; atomic write |
| `subtitle_lifecycle.py` | VTT/SRT delete after success; quarantine on parse failure; legacy `yt_subs` dry-run cleanup |
| `claim_manager.py` | `tmp/claims/<video_id>.json` atomic claim; stale recovery |
| `shared_state_writer.py` | Serial apply of `VideoProcessResult` to shared files |
| `runtime_resources.py` | Device route semaphore (Whisper MLX + future on-device LLM, concurrency=1) |
| `preprocess_backend.py` | `cloud_api` (GPT Nano) / `on_device` stub seam |
| `admission_limiter.py` | Shared download admission + provider cooldown |

**Concurrency contract:** `VIDEO_WORKERS=2`, `DEVICE_COMPUTE_CONCURRENCY=1`, `PREPROCESS_BACKEND=cloud_api`. On-device preprocessing is reserved/disabled.

**Rollback:** Set `VIDEO_WORKERS=1` in env; revert to tag before v4.2 merge if needed.

### Validation

```bash
conda run -n ai pytest tests/
conda run -n ai python -m compileall .
```

---

## v4.1.3 — Insights / Key Takeaways role split (2026-07-20)

Prompt-only + export helpers. New pipeline runs get richer callouts; existing notes unchanged.

| Area | Change |
|------|--------|
| `main.py` `INPUT_PROMPT` | **v4:** 한눈에 보기 = `[확정]`/`[정황]` only; Insights 2~4 (`[외부지식]`/`[추정]`); Key Takeaways 3~5 so-what; anti-duplication |
| `stt_function_v3.py` | `MERGE_SUMMARY_PROMPT` aligned with callout depth/roles |
| `scripts/md_mobile_utils.py` | `extract_callout(body, label)` |
| `scripts/export_blog_candidates.py` | `context_summary` fills `[해석]` / `[시사점]` from MD callouts (legacy placeholder if missing) |

**Unchanged:** v4.1 section order, A4 grounding tags, catalog schema, no bulk re-summarize.

### Validation

```bash
python -m py_compile main.py stt_function_v3.py scripts/md_mobile_utils.py scripts/export_blog_candidates.py
python scripts/test_md_mobile_utils.py
```

---

## 실행 상태 (2026-06-28)

| Phase | 내용 | 상태 |
|-------|------|------|
| 0 | `note_catalog.jsonl`, audit, `digest/` | ✅ 완료 (~24k rows) |
| 1.5 | 30일 frontmatter backfill | ✅ 1,504건 (+ VID fix `--force`) |
| 1a | `INPUT_PROMPT` v2, `prompt_structure` 슬림화 | ✅ |
| 1b | YAML 4.1 + catalog append + daily digest | ✅ |
| 1c | nano retention / skip merge reminimize | ✅ |
| 2+ | C1/C2, B4/B5, MOC, 임베딩 | ⏸ 보류 |

### 검증 (2026-06-28)

| 항목 | 결과 |
|------|------|
| Pilot 2건 (`run_mobile_md_pilot.py`) | ✅ `한눈에 보기`, callout, Tags, mermaid/부족한점 없음 |
| E2E 스모크 (`logs/smoke_test/smoke_v411_full.md`) | ✅ `format_version: 4.1` + tags + tldr |
| Digest | ✅ `002_YT_Script/digest/2026_06_28.md` (13 rows) |
| Kickstart | ✅ launchd 재시작; **v4.1 MD는 kickstart 이후 신규 처리분부터** |
| Obsidian iOS | 수동 확인 (선택) |

> **혼합 볼트:** Phase 1.5 backfill = `format_version: 4.0` + `_5-mini` 본문. kickstart 이전 `_dS4f` = v1 프롬프트 본문 + 4.0 YAML. **이후 신규 `_dS4f`만** v4.1 풀포맷.

---

## v4.1.1 — Phase 1c nano 절감 (2026-06-28)

- **`config.py`:** `NANO_RETENTION_AUTO_SUBS=(60,80)`, `NANO_RETENTION_DEFAULT=(80,95)`, `SKIP_MERGE_REMINIMIZE=True`
- **`build_token_query()`** (`main.py`): auto_subs 시 한국어 filler 제거 + retention 분기
- **`token_minimizer_chunked()`:** merge 후 재압축 1회 생략 (`skip_merge_reminimize`)
- 로그: `Nano retention: 60~80% (auto_subs)`

### 예상 절감 (월 ~600건, baseline ~$27)

| 항목 | 월 절감 |
|------|---------|
| auto_subs retention 80~95%→60~80% | ~$3~4 |
| SKIP_MERGE_REMINIMIZE (긴 영상) | ~$0.5~1 |
| **합계** | **~$3.5~5/월 (~13~18%)** |

DeepSeek 요약(~$2/월)은 거의 불변. auto_subs retention 과도 시 요약 누락 가능 → 2주 모니터링 권장.

---

## v4.1.0 — Phase 1a/1b (2026-06-28)

### 프롬프트 (1a)

- **`INPUT_PROMPT` v2:** `## 한눈에 보기`, callout (`> [!note]-`), `## Tags`, A4 `[확정/정황/추정/외부지식]`, mermaid·`부족한 점` 금지
- **`prompt_structure()`:** token/language wrapper만 (본문 규칙은 `INPUT_PROMPT` 단일 원장)
- **`MERGE_SUMMARY_PROMPT`:** chunk merge 시 모바일 섹션 구조 유지
- **`PRE_TASK_TYPE`:** `nano_preprocess`

### 파이프라인 (1b)

| Path | Role |
|------|------|
| `scripts/md_mobile_utils.py` | Tags 파싱, tldr/title 추출, YAML+본문 조립 |
| `main.py` Step 8 | `format_version: 4.1` frontmatter; `영상 업로드 일자:` 헤더 제거 |
| `scripts/note_catalog_utils.py` | `append_catalog_entry()`, frontmatter `tags`/`title`/`tldr` |
| `scripts/build_daily_digest.py` | `digest/YYYY_MM_DD.md` (LLM $0) |
| `main.py` `process_videos()` | 배치 종료 시 digest 자동 갱신 |
| `experiments/run_mobile_md_pilot.py` | Phase 1a pilot (blind test 2건) |
| `scripts/retry_small_summary_auto_subs.py` | 동일 MD 조립 + 1c retention |

### Frontmatter schema (v4.1 — pipeline output)

```yaml
---
format_version: "4.1"
vid: h3AVpzV7iMQ
channel: UncleLee
upload_date: 2026-06-27
transcript_date: 2026-06-28
tags: [crypto, solana, fintech]
title: ...
tldr: ...
lang: ko
suffix: dS4f
source_url: https://www.youtube.com/watch?v=...
---
```

### 목표 MD 본문 구조

```markdown
# {title}

## 한눈에 보기
- [정황] ...

## 핵심 요점
...

> [!note]- Insights
> ...

> [!note]- Key Takeaways
> ...

## 용어          # 선택
```

(`## Tags`는 파싱 후 frontmatter로 이동, 본문에서 제거)

---

## v4.0.0 — Mobile catalog & frontmatter (Phase 0 + 1.5)

Date: 2026-06-28

### Added

| Path | Role |
|------|------|
| `scripts/note_catalog_utils.py` | Shared catalog + frontmatter helpers |
| `scripts/build_note_catalog.py` | Phase 0 — `YTT_AUDIO/index/note_catalog.jsonl` |
| `scripts/audit_note_catalog.py` | Phase 0 — gap report JSON |
| `scripts/backfill_frontmatter_recent.py` | Phase 1.5 — YAML on recent MD (`--force` for re-apply) |

### Outputs (local, not iCloud)

- `{P03}/index/note_catalog.jsonl` (~11MB, ~24k rows) — 2026-07+ unified layout (`~/Developer/PJT/p03_speech2text/index/`)
- `{P03}/index/note_catalog_audit.json`
- Obsidian `002_YT_Script/digest/`

### Frontmatter schema (v4.0 — backfill only)

```yaml
---
format_version: "4.0"
vid: ...
channel: ...
upload_date: YYYY-MM-DD
transcript_date: YYYY-MM-DD
lang: ko
suffix: 5-mini
source_url: https://youtube.com/watch?v=...
---
```

---

## Usage

```bash
cd ~/Developer/PJT/p03_speech2text

# Phase 0
python scripts/build_note_catalog.py
python scripts/audit_note_catalog.py

# Phase 1.5
python scripts/backfill_frontmatter_recent.py --days 30 --dry-run
python scripts/backfill_frontmatter_recent.py --days 30 --apply
python scripts/backfill_frontmatter_recent.py --days 30 --apply --force  # VID fix re-apply

# Phase 1a pilot
conda run -n ai python experiments/run_mobile_md_pilot.py

# Phase 1b digest
python scripts/build_daily_digest.py
python scripts/build_daily_digest.py --date 2026-06-28 --dry-run

# Pipeline (launchd or manual)
python main.py
launchctl kickstart -k gui/$(id -u)/com.user.p03-speech2text
```

---

## Content reuse (blog / AI·투자, R0/R1)

- [CONTENT_REUSE_PLAN.md](CONTENT_REUSE_PLAN.md) — 활용 기획서
- [NOTIFICATION_SPEC.md](NOTIFICATION_SPEC.md) — 알림 spec (R3 deferred)
- `scripts/export_blog_candidates.py` — catalog → blog CSV
- [p02_blog YT_SOURCE_GUIDE.md](../../p02_blog/docs/YT_SOURCE_GUIDE.md)

```bash
python scripts/export_blog_candidates.py --days 7 --tags ai,crypto,llm --dry-run
```

---

## Deferred (Phase 2+)

- C1/C2 관련링크·주간요약
- B4 YouTube `?t=` 타임스탬프
- B5 채널 MOC
- 20k 임베딩 백필
- 20k / 30일 bulk 재요약

---

## 비용 요약 (월 ~600건)

| 항목 | 월 비용 |
|------|---------|
| Baseline (nano + DeepSeek) | ~$27 |
| Phase 1a/1b 출력 풍성화 | +$6~8 |
| Phase 1c nano 절감 | −$3.5~5 |
| **현재 추정** | **~$28~32** |
