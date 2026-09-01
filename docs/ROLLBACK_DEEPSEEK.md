# DeepSeek 전환 롤백 (요약)

전체 백업 스냅샷은 로컬 `backup/pre_deepseek_20260628_0027/` 에만 있습니다 (git 미포함).

`.env`에서 아래로 되돌리면 Mini 단일 primary 복구 (fallback 비활성):

```env
MAIN_LLM_PROVIDER=openai
MAIN_LLM_MODEL=gpt-5-mini-2025-08-07
MAIN_LLM_OUTPUT_SUFFIX=5-mini
# MAIN_LLM_FALLBACK_PROVIDER=
# MAIN_LLM_FALLBACK_MODEL=
```

Mini primary + DeepSeek fallback만 쓰려면 [UPDATE_LOG_20260629_llm_fallback.md](UPDATE_LOG_20260629_llm_fallback.md) 참고.

코드 파일 복원은 로컬 `backup/pre_deepseek_20260628_0027/ROLLBACK.md` 참고.
