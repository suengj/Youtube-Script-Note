# 업데이트 로그 (2026-06-29)

메인 요약 LLM **primary / fallback** 라우팅 추가.

> **선행:** DeepSeek 전환 — [UPDATE_LOG_20260628.md](UPDATE_LOG_20260628.md)

---

## 1. 배경

- 메인 요약은 2026-06-28에 **DeepSeek V4 Flash** (OpenRouter)로 전환됨.
- 이후 **다른 모델을 primary로 바꾸고**, 장애·rate limit 시 **DeepSeek를 자동 fallback**으로 쓰고 싶은 요구.
- `.env`만으로 primary/fallback 조합을 바꿀 수 있도록 코드 정리.

---

## 2. `.env` 설정

### 현재 기본 (DeepSeek primary + DeepSeek fallback)

primary와 fallback이 같아도 동작에는 문제 없음. primary를 바꿀 때 fallback이 의미를 가짐.

```env
MAIN_LLM_PROVIDER=openrouter
MAIN_LLM_MODEL=deepseek/deepseek-v4-flash
MAIN_LLM_OUTPUT_SUFFIX=dS4f

MAIN_LLM_FALLBACK_PROVIDER=openrouter
MAIN_LLM_FALLBACK_MODEL=deepseek/deepseek-v4-flash

PREPROCESS_LLM_MODEL=gpt-5-nano-2025-08-07
OPENROUTER_API_KEY=...
OPENAI_API_KEY=...
```

### 예: GPT-5 Mini primary + DeepSeek fallback

```env
MAIN_LLM_PROVIDER=openai
MAIN_LLM_MODEL=gpt-5-mini-2025-08-07
MAIN_LLM_OUTPUT_SUFFIX=5-mini

MAIN_LLM_FALLBACK_PROVIDER=openrouter
MAIN_LLM_FALLBACK_MODEL=deepseek/deepseek-v4-flash
```

| 변수 | 설명 |
|------|------|
| `MAIN_LLM_PROVIDER` | primary API: `openai` \| `openrouter` |
| `MAIN_LLM_MODEL` | primary 모델 ID |
| `MAIN_LLM_FALLBACK_PROVIDER` | (선택) fallback API |
| `MAIN_LLM_FALLBACK_MODEL` | (선택) fallback 모델 ID — 설정 시 provider도 필수 |
| `MAIN_LLM_OUTPUT_SUFFIX` | MD/txt 파일명 suffix (`_dS4f`, `_5-mini` 등) |

fallback 미설정 시 이전과 동일하게 primary만 사용.

---

## 3. 동작

1. `initialize_clients()` → `MainLlmConfig` (primary client/model + optional fallback).
2. `main_llm.summarize()` → `summarize_with_chunking()` → `response_text_5mini()`.
3. primary 호출 실패 시 **재시도 가능 오류**면 fallback 1회 시도:
   - rate limit, timeout, connection error, 5xx
   - invalid model / 400 (모델 ID 오류)
4. chunked 요약 시 **청크·merge 각 호출**마다 동일 fallback 적용.
5. 로그: `LLM config: ... fallback=...` 및 `Primary LLM failed ... retrying fallback`.

출력 suffix는 primary 설정(`MAIN_LLM_OUTPUT_SUFFIX`)을 따름 — fallback 성공해도 파일명은 동일.

---

## 4. 코드 변경

| 파일 | 변경 |
|------|------|
| `main.py` | `MainLlmConfig`, `MAIN_LLM_FALLBACK_*`, `_create_llm_provider_client()` |
| `stt_function_v3.py` | `_is_retryable_llm_error()`, `response_text_5mini()` fallback retry |
| `scripts/retry_small_summary_auto_subs.py` | `main_llm.summarize()` |
| `experiments/run_mobile_md_pilot.py` | 동일 |
| `scripts/smoke_test_main_llm.py` | primary + forced-failure fallback 테스트 |
| `scripts/smoke_test_deepseek.py` | 위 스크립트 thin wrapper |
| `.env.example` | fallback 예시 |

---

## 5. 검증

```bash
conda run -n ai python scripts/smoke_test_main_llm.py
```

- `PRIMARY: PASS` — 실제 primary 모델로 요약
- `FALLBACK: PASS` — 잘못된 primary 모델 → fallback 성공

로그 예:

```
LLM config: preprocess=gpt-5-nano-... main=deepseek/deepseek-v4-flash (openrouter) fallback=deepseek/deepseek-v4-flash (openrouter) output_suffix=_dS4f
```

---

## 6. 롤백

fallback만 끄기:

```env
# MAIN_LLM_FALLBACK_PROVIDER=
# MAIN_LLM_FALLBACK_MODEL=
```

(두 줄 삭제 또는 비우기)

Mini 단일 primary로 완전 복구: [ROLLBACK_DEEPSEEK.md](ROLLBACK_DEEPSEEK.md)
