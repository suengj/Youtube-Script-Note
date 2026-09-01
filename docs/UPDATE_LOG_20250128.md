# 업데이트 로그 (2025-01-28)

## 1. 자동 자막 파일명 불일치 수정

### 문제
- yt-dlp는 여러 언어 자막을 `{video_id}.{lang}.vtt` 형식으로 저장 (예: `abc123.en.vtt`, `abc123.ko.vtt`)
- 기존 코드는 `{video_id}.vtt`만 탐색 → 자막을 찾지 못해 항상 Whisper로 폴백

### 수정 (stt_function_v3.py)
- `_yt_download_subs_only`, `_yt_download_auto_subs_only`: `subs_langs` 순서대로 `{video_id}.{lang}.vtt` 탐색
- 존재하는 첫 파일 반환, 없으면 기존 형식 `{video_id}.vtt` fallback

```python
# 수정 후 탐색 순서
for lang in langs:
    for ext in (".vtt", ".srt"):
        p = os.path.join(subs_path, f"{video_id}.{lang}{ext}")
        if os.path.isfile(p):
            return p
for ext in (".vtt", ".srt"):
    p = os.path.join(subs_path, f"{video_id}{ext}")
    if os.path.isfile(p):
        return p
return None
```

---

## 2. 일본어(jp) 지원 및 언어 코드 정규화

### _parse_subs_langs (stt_function_v3.py)
- YouTube/yt-dlp는 ISO 639-1 `ja` 사용 (일본어), `jp`는 국가 코드
- `jp` → `ja` 자동 매핑 추가

```python
normalized = ["ja" if p.lower() == "jp" else p for p in parts]
```

### config.py
- `YOUTUBE_SUBS_LANGS = "en,ko,jp,ja,en-US,en-GB"` (jp, ja 둘 다 포함 가능)

### .env
- 선택적 오버라이드 예시 추가:
  - `YOUTUBE_AUTO_SCRIPT=true`
  - `YOUTUBE_SUBS_LANGS="en,ko,jp,en-US,en-GB"`

---

## 3. Sleep time 및 Block 가능성 검토

### 결론
- `time.sleep()`은 의도된 rate limiting (YouTube IP block 방지)
- 데드락/무한 대기 위험 없음
- launchd/cron 환경에서도 정상 동작

---

## 4. 백업

- **경로**: `backup/backup_20250128.zip`
- **포함 파일**: config.py, main.py, stt_function_v3.py, channel_crawl.py, md_relocate.py, run_lock.py, zip_process.py, output_df_new.csv, crawl_yt_list.csv, channel_df.csv, input_df.csv, output_일자별 체크.csv

---

## 5. VTT 파일 크기 (참고)

- VTT는 재생용 포맷이라 텍스트 대비 2~3배 이상 큼
- 타임스탬프, 인라인 `<c>...</c>` 태그, 메타데이터 포함
- `subtitle_file_to_plain_text()`가 메타데이터 제거 후 텍스트만 추출

---

## 6. 기획서 (미구현)

- **YouTube API defaultAudioLanguage**: 영상 주 언어 기반 자막 선택 — 2·3단계로 보류
- **권장**: 1단계(파일명 수정)만 적용 완료. 필요 시 defaultAudioLanguage 연동 검토

---

## 수정된 파일 목록

| 파일 | 변경 내용 |
|------|-----------|
| stt_function_v3.py | `_parse_subs_langs` jp→ja 매핑, `_yt_download_subs_only`/`_yt_download_auto_subs_only` 파일 탐색 로직 |
| config.py | YOUTUBE_SUBS_LANGS에 jp, ja 포함 |
| .env | YOUTUBE_AUTO_SCRIPT, YOUTUBE_SUBS_LANGS 주석 예시 |
| docs/PROJECT.md | 자막 우선순위, YOUTUBE_AUTO_SCRIPT, YOUTUBE_SUBS_LANGS 설명 |
| docs/UPDATE_LOG_20250128.md | 본 로그 (신규) |
