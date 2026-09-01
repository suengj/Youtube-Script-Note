# 업데이트 로그 (2026-06-28)

DeepSeek V4 Flash 전환, 로컬 경로 정본화, LLM 블라인드 파일럿 결과 반영.

> **후속 (v4.1.1):** Obsidian mobile MD Phase 0~1c — [UPDATE_LOG_v4_mobile_catalog.md](UPDATE_LOG_v4_mobile_catalog.md)  
> **후속 (2026-06-29):** 메인 LLM primary/fallback — [UPDATE_LOG_20260629_llm_fallback.md](UPDATE_LOG_20260629_llm_fallback.md)

---

## 1. 배경

### iCloud Errno 11

- iCloud Drive 경로에서 CSV read/write 시 `OSError [Errno 11]` (동기화 잠금) 발생
- **대응 (기존 P1)**: `WORK_PATH=$LEGACY_WORK_PATH` → `DATA_ROOT=YTT_AUDIO/data` 로 핫 CSV 분리
- **이번 추가**: `BASE_PATH`도 로컬 `~/Documents/Code/Python/PJT/p03_speech2text` 로 변경 → `output_new/` 등 런타임 I/O가 iCloud를 거치지 않음

### LLM 블라인드 파일럿 (2 VTT × 3모델)

산출물: `experiments/llm_blind_test_20260616/`

| 샘플 | 1순위 | 제외 |
|------|-------|------|
| Bioethics | **DeepSeek V4 Flash** | Gemini 2.5 Flash (줄글 과다) |
| Mexico/China | **DeepSeek V4 Flash** | Gemini 2.5 Flash |

→ Main LLM을 **DeepSeek V4 Flash** (OpenRouter)로 전환.

---

## 2. 경로 아키텍처 (변경 후)

```
~/Documents/Code/Python/PJT/p03_speech2text/   ← 코드 실행·BASE_PATH (로컬)
        │
        ├── output_new/     ← 전사 txt (로컬)
        ├── logs/             ← stt 로그 (로컬)
        │
        │  WORK_PATH
        ▼
~/YTT_AUDIO/
├── data/          ← CSV 정본 (output_df, channel_df, crawl queue)
├── audio/ yt_subs/ tmp/ cache/ prompt/
        │
        │  mirror (선택, 단방향)
        ▼
iCloud .../p03_speech2text/   ← 코드·CSV 백업 사본 (런타임 정본 아님)

OUTPUT_MD_PATH → Obsidian (iCloud) — MD만 출력, atomic_write + retry 유지
```

---

## 3. LLM 설정 (`.env`)

```env
BASE_PATH="$PROJECT_ROOT"
WORK_PATH="$LEGACY_WORK_PATH"

MAIN_LLM_PROVIDER=openrouter
MAIN_LLM_MODEL=deepseek/deepseek-v4-flash
MAIN_LLM_OUTPUT_SUFFIX=dS4f
MAIN_LLM_FALLBACK_PROVIDER=openrouter
MAIN_LLM_FALLBACK_MODEL=deepseek/deepseek-v4-flash
PREPROCESS_LLM_MODEL=gpt-5-nano-2025-08-07
OPENROUTER_API_KEY=...   # ~/.hermes/profiles/academia/.env 와 동일
```

| 단계 | 모델 | API |
|------|------|-----|
| 전처리 (Nano) | `gpt-5-nano-2025-08-07` | OpenAI 직결 |
| 메인 요약 (primary) | `deepseek/deepseek-v4-flash` | OpenRouter |
| 메인 요약 (fallback, 선택) | `deepseek/deepseek-v4-flash` | OpenRouter |

fallback 상세: [UPDATE_LOG_20260629_llm_fallback.md](UPDATE_LOG_20260629_llm_fallback.md)

MD 파일명 suffix: **`_dS4f`** (예: `채널명_제목_dS4f.md`)

---

## 4. 코드 변경 요약

| 파일 | 변경 |
|------|------|
| `main.py` | `LOCAL_BASE_PATH_DEFAULT`, LLM env 키, `initialize_clients()` → OpenRouter, `main_llm_client`, suffix `_dS4f` |
| `stt_function_v3.py` | `response_text_5mini` — 토큰·추정 비용 로그 |
| `scripts/retry_small_summary_auto_subs.py` | 동일 client/model/suffix |
| `.env` | 위 LLM·경로 설정 |

프로젝트 루트 낡은 CSV → `backup/legacy_icloud_csv_20260628/` 이동 (정본은 `YTT_AUDIO/data`).

---

## 5. 비용 (참고)

| 구성 | 건당 추정 (40K in / 55K out) |
|------|------------------------------|
| Nano + GPT-5 Mini (이전) | ~$0.144 |
| Nano + DeepSeek (현재) | ~$0.045 (~69% 절감) |

파일럿 실측 (summarize만): Mini $0.015 vs DeepSeek $0.002.

---

## 6. 백업·롤백

- `backup/pre_deepseek_20260628_0027/` — 전환 직전 스냅샷
- `backup/py_backup_pre_deepseek_20260628.zip`
- `backup/ROLLBACK_DEEPSEEK.md` — 복원 명령

```bash
BK="$PROJECT_ROOT/backup/pre_deepseek_20260628_0027"
cp "$BK"/{main.py,stt_function_v3.py,config.py,.env} "$PROJECT_ROOT/"
cp "$BK/scripts/retry_small_summary_auto_subs.py" ".../scripts/"
```

`.env`에서 `MAIN_LLM_PROVIDER=openai`, `MAIN_LLM_MODEL=gpt-5-mini-2025-08-07`, `MAIN_LLM_OUTPUT_SUFFIX=5-mini` 로 되돌리면 Mini 복구. fallback 줄은 삭제하거나 비우기.

primary는 Mini·fallback은 DeepSeek 조합: [UPDATE_LOG_20260629_llm_fallback.md](UPDATE_LOG_20260629_llm_fallback.md)

---

## 7. iCloud·GitHub

- **GitHub**: [suengj/p03_speech2text](https://github.com/suengj/p03_speech2text) (private). MD 웹뷰는 [suengj/md_reader](https://github.com/suengj/md_reader).
- **iCloud 코드 동기화**: `scripts/mirror_code_to_icloud.py` (로컬 → iCloud 프로젝트 폴더, 단방향)
- **iCloud 데이터 동기화**: `scripts/mirror_data_root_to_icloud.py` (`YTT_AUDIO/data` → `ICLOUD_MIRROR_PATH`, 기본 iCloud 프로젝트 폴더)

---

## 8. 검증

스모크 테스트: `scripts/smoke_test_main_llm.py` (`smoke_test_deepseek.py`는 호환 래퍼)

로그 확인: `logs/stt_YYYYMMDD.log` 에 `LLM usage model=...` 및 `LLM config: preprocess=... main=... fallback=...` 출력.
