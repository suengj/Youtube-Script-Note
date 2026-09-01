# Auto-Subs (Live Caption) 단일 언어 다운로드 기획

> **구현 완료** (2026-01-28). stt_function_v3.py에 `_resolve_primary_lang`, `_yt_download_auto_subs_only(info=)` 적용.

## 1. 현재 프로세스 분석

### 1.1 흐름 요약

```
[main.py] video_config에 default_audio_lang, auto_subs_only 설정
    ↓
[stt_function_v3.yt_downloader_ytdlp]
  - extract_info(URL, download=False) → info
  - has_auto_captions = bool(info.get("automatic_captions"))
  - prefer_lang = config.get("default_audio_lang")  # channel crawl에서만 존재 가능
  - _yt_download_auto_subs_only(URL, video_id, subs_dir, subs_langs, prefer_lang)
    ↓
[_yt_download_auto_subs_only]
  - langs = [prefer_lang] + [x for x in subs_langs if x != prefer_lang]  # prefer_lang 우선
  - ydl_opts["subtitleslangs"] = langs  # ["en","ko","ja","en-US","en-GB"] 등 전체
  - ydl.download([URL])  # yt-dlp가 langs에 있는 **모든** 언어 자막 다운로드
  - 첫 번째로 존재하는 파일 반환 (prefer_lang → subs_langs 순)
```

### 1.2 문제점

| 항목 | 현재 | 영향 |
|------|------|------|
| **다운로드 대상** | `YOUTUBE_SUBS_LANGS` 전체 (en, ko, ja, en-US, en-GB) | 영상당 3~5개 언어 자막 파일 다운로드 |
| **yt-dlp 동작** | `subtitleslangs: [en, ko, ja, ...]` → **모든** 언어 다운로드 | 용량·대역폭 낭비 |
| **선택 로직** | 다운로드 후 `prefer_lang` 우선, 없으면 순서대로 첫 번째 존재 파일 사용 | 실제 사용은 1개뿐 |

### 1.3 관련 파일

| 파일 | 역할 |
|------|------|
| `stt_function_v3.py` | `_yt_download_auto_subs_only`, `yt_downloader_ytdlp` |
| `main.py` | `YOUTUBE_SUBS_LANGS`, `url_to_default_audio_lang` 전달 |
| `channel_crawl.py` | `default_audio_lang` (YouTube API `defaultAudioLanguage`) 수집 |
| `config` | `YOUTUBE_SUBS_LANGS`: `en,ko,jp,en-US,en-GB` |

### 1.4 default_audio_lang 출처

- **channel crawl (shorts_seconds_threshold > 0)**: `_fetch_video_durations` → YouTube API `videos.list` → `snippet.defaultAudioLanguage`
- **channel crawl (shorts_seconds_threshold = 0)**: API 호출 없음 → `default_audio_lang` 빈 문자열
- **input_df**: meta 없음 → `url_to_default_audio_lang` 빈

---

## 2. 목표: 주 언어 1개만 다운로드

### 2.1 원칙

1. **주 언어 선정**: 영상의 주 언어를 먼저 결정
2. **단일 다운로드**: 해당 언어 자막만 다운로드
3. **용량 절감**: 기존 대비 약 1/3~1/5 수준 (en+ko+ja → 1개)

### 2.2 주 언어 결정 우선순위

| 순위 | 출처 | 조건 |
|------|------|------|
| 1 | `config.default_audio_lang` (prefer_lang) | 설정되어 있고, `automatic_captions`에 해당 키 존재 |
| 2 | `info.automatic_captions` + `subs_langs` | `subs_langs` 순서대로, `automatic_captions`에 존재하는 첫 언어 |
| 3 | `info.automatic_captions` 첫 키 | fallback (YouTube가 제공하는 첫 번째 자동 자막) |

---

## 3. 구현 계획

### 3.1 Phase 1: `_yt_download_auto_subs_only` 개선

**변경 사항**

1. **인자 추가**: `info=None` (yt-dlp `extract_info` 결과)
2. **주 언어 결정**:
   ```python
   def _resolve_primary_lang(info, prefer_lang, subs_langs):
       ac = (info or {}).get("automatic_captions") or {}
       if not ac:
           return None
       # 1) prefer_lang이 있고 ac에 존재
       if prefer_lang:
           base = prefer_lang.split("-")[0]  # en-US → en
           for k in ac:
               if k == prefer_lang or k.startswith(base + "-"):
                   return k
           if prefer_lang in ac or base in ac:
               return prefer_lang if prefer_lang in ac else base
       # 2) subs_langs 순서대로 ac에 존재하는 첫 언어
       for lang in subs_langs:
           base = lang.split("-")[0]
           for k in ac:
               if k == lang or k.startswith(base + "-"):
                   return k
       # 3) ac의 첫 키
       return next(iter(ac.keys()), None)
   ```
3. **다운로드**: `subtitleslangs: [primary_lang]` (1개만)
4. **info 없을 때**: 기존 동작 유지 (전체 langs로 다운로드) — 하위 호환

### 3.2 Phase 2: `yt_downloader_ytdlp`에서 info 전달

**변경 사항**

- `_yt_download_auto_subs_only` 호출 시 `info=info` 전달
- `extract_info`는 이미 호출 중이므로 추가 요청 없음

### 3.3 Phase 3: default_audio_lang 확대 (선택)

**현재**: `shorts_seconds_threshold > 0`일 때만 API로 `default_audio_lang` 수집

**옵션 A**: shorts 여부와 관계없이 `_fetch_video_durations` 호출  
- 장점: 모든 영상에 `default_audio_lang` 사용 가능  
- 단점: API quota 증가

**옵션 B**: 유지  
- `default_audio_lang` 없으면 `info.automatic_captions` 기반으로만 선택  
- API 호출 수 유지

**권장**: Phase 1~2만 적용, Phase 3은 옵션 B로 유지

---

## 4. 파일별 수정 요약

| 파일 | 수정 내용 |
|------|----------|
| `stt_function_v3.py` | `_resolve_primary_lang()` 추가, `_yt_download_auto_subs_only(info=, ...)` 수정, `yt_downloader_ytdlp`에서 `info` 전달 |

---

## 5. 예상 효과

| 지표 | 현재 | 변경 후 |
|------|------|---------|
| 영상당 자막 파일 수 | 3~5개 (en, ko, ja 등) | 1개 |
| 다운로드 용량 | ~3~5배 | ~1배 |
| 네트워크 트래픽 | 동일 비율로 감소 | |

---

## 6. 주의사항

1. **YouTube 자막 키**: `en`, `en-US`, `en-GB` 등 변형 존재 → `startswith` 등으로 매칭
2. **jp vs ja**: config는 `jp`, YouTube는 `ja` 사용 → 기존 `_parse_subs_langs` 매핑 유지
3. **automatic_captions 없음**: 일부 영상은 자동 자막 미제공 → 기존 fallback 로직 유지
