# 업데이트 로그 (2026-01-28)

## 1. VID 항상 파일명에 포함

### 문제
- 기존 전사 프로세스에서는 제목 길이 한도가 없을 때 VID가 포함되어 있었음
- Subs 경로에서 `base_name`이 제목만 사용되어 VID가 누락되는 경우 발생

### 수정 (main.py)
- `base_name` 생성 시 `video_id`를 항상 append: `base_name = f"{base_name}_{video_id}"` (VID가 아직 없을 때만)
- `FILENAME_MAX_LENGTH`(config)로 제목 최대 길이 제어 가능 (0이면 제한 없음)

**결과 형식:** `{sanitized_title}_{video_id}_{lang}_subs.txt` 또는 `{sanitized_title}_{video_id}_{lang}_auto_subs.txt`

---

## 2. auto_subs 시 full 저장 생략 (모드 off)

### 요구사항
- auto_subs 사용 시 full 버전 처리가 제대로 되지 않아, full 저장을 스킵
- 코드 원복이 아닌 config 플래그로 제어 (향후 full 프로세싱 개선 예정)

### 수정 (config.py, main.py)
- `SAVE_FULL_WHEN_AUTO_SUBS = False` (기본값) 추가
- `subs_source == "auto"`이고 `SAVE_FULL_WHEN_AUTO_SUBS=False`일 때 `output_new/full/` 저장 생략
- `.env`에서 `SAVE_FULL_WHEN_AUTO_SUBS=true`로 오버라이드 가능

---

## 3. VID-only → YouTube 제목 일괄 변경 (vid_to_title_rename.py)

### 목적
- 이미 VID로만 저장된 파일명(summary, Obsidian MD)을 YouTube 제목을 조회하여 `{title}_{VID}_*` 형식으로 변경

### 신규 스크립트
- `vid_to_title_rename.py`: `output_new/summary/`, `OUTPUT_MD_PATH`에서 VID-only 패턴 파일 탐색
- yt-dlp로 각 VID의 YouTube 제목 조회 후 `sanitize_filename` 적용하여 rename
- **처리 대상**: `{VID}_suffix.txt` 형식만. Whisper 형식 `{title}+vid-{VID}_*.txt`는 스킵 (섹션 5 참고)
- `--dry-run`: 계획만 출력
- `--base-path`: 프로젝트 경로 지정

```bash
python vid_to_title_rename.py --dry-run
python vid_to_title_rename.py
```

---

## 4. md_relocate 주간 plist

### 목적
- `md_relocate.py`를 매주 일요일 00:00에 자동 실행

### 신규 plist
- `Documents/Code/launchd/com.user.p03-md-relocate.plist`
- `StartCalendarInterval`: Weekday=1(Sunday), Hour=0, Minute=0

**터미널 명령:**
```bash
# 심볼릭 링크 (최초 1회)
ln -s $PROJECT_ROOT/launchd/com.user.p03-md-relocate.plist ~/Library/LaunchAgents/com.user.p03-md-relocate.plist

# 등록
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.p03-md-relocate.plist

# 강제 실행 (테스트)
launchctl kickstart -k gui/$(id -u)/com.user.p03-md-relocate

# 해제
launchctl bootout gui/$(id -u)/com.user.p03-md-relocate
```

---

## 5. vid_to_title_rename.py — Whisper 형식 파일 스킵

### 문제
- Whisper 경로 형식 `{title}+vid-{VID}_*.txt` (예: `Melchizedek_ The Most...m4a+vid-kEruf1XhdpA_4o-mini.txt`)은 이미 제목이 포함됨
- 기존 VID_PATTERN `^([A-Za-z0-9_-]{11})`이 맨 앞 11자(`Melchizedek_`)를 VID로 잘못 추출 → yt-dlp "Video unavailable" 에러

### 수정 (vid_to_title_rename.py)
- `extract_vid_from_summary_name()`: 파일명에 `+vid-`가 있으면 (None, None) 반환 → 스킵
- `extract_vid_from_md_name()`: 동일하게 `+vid-`가 있으면 스킵
- **처리 대상**: VID-only 형식만 (`xHi8PUIVyoo_auto_subs_5-mini.txt` 등)
- **스킵 대상**: Whisper 형식 (`Title+vid-VID_suffix.txt`) — 이미 제목 포함

---

## 6. auto_sub_only 채널 지원

### 목적
- `channel_df.csv`의 `auto_sub_only` 컬럼에 값이 있을 경우(예: "AutoSub"), 해당 채널 영상은 **자막 OR 자동 자막**이 있을 때만 VTT로 전사
- 둘 다 없으면 `skipped_auto_subs_only`로 스킵 (음원 다운로드/Whisper 미실행)
- **auto_sub_only가 비어 있으면** 기존대로 진행

### 수정
- **channel_crawl.py**: CHANNEL_DF_COLUMNS·CRAWL_QUEUE_COLUMNS에 `auto_sub_only` 추가, load/save/build_queue에서 전달
- **stt_function_v3.py**: `yt_downloader_ytdlp`에서 `auto_subs_only`이고 자막·자동자막 둘 다 없으면 `__SKIP_AUTO_SUBS_ONLY__` 반환
- **main.py**: meta_for_channel_crawl에 `auto_subs_only`, video_config 전달, sentinel 처리, 통계·Rate limiting
- **channel_crawl.py**: `skipped_auto_subs_only`를 queue `done`으로 처리 (reconcile, apply_result)

---

## 7. md 저장: 날짜 폴더에 직접 저장

### 변경
- **기존:** flat 형식 `YYYY-MM-DD_파일명.md` 저장 후 md_relocate 주간 배치로 날짜 폴더 이동
- **변경:** 저장 시 `OUTPUT_MD_PATH/YYYY_MM_DD/파일명.md`에 직접 저장. 폴더 없으면 `os.makedirs` 생성
- **md_relocate plist:** 해제됨 (주간 배치 불필요)
- **vid_to_title_rename:** 날짜 서브폴더 스캔 (`YYYY_MM_DD/VID_suffix.md`) 지원 추가

---

## 8. 자동 자막(live caption) 단일 언어 다운로드

### 목적
- 기존: `YOUTUBE_SUBS_LANGS` 전체(en, ko, ja 등)를 다운로드 후 첫 번째 존재 파일 사용 → 용량·대역폭 낭비
- 변경: 영상 주 언어를 먼저 결정하고, 해당 언어 1개만 다운로드

### 주 언어 결정 우선순위
1. `config.default_audio_lang` (channel crawl에서 YouTube API `defaultAudioLanguage` 수집) — `automatic_captions`에 존재 시
2. `info.automatic_captions` + `subs_langs` — `subs_langs` 순서대로 ac에 존재하는 첫 언어
3. `info.automatic_captions` 첫 키 (fallback)

### 수정 (stt_function_v3.py)
- `_resolve_primary_lang(info, prefer_lang, subs_langs)`: 주 언어 결정
- `_yt_download_auto_subs_only(..., info=None)`: info 전달 시 `subtitleslangs: [primary_lang]`로 1개만 다운로드
- `yt_downloader_ytdlp`: `_yt_download_auto_subs_only` 호출 시 `info=info` 전달

### 예상 효과
- 영상당 자막 파일: 3~5개 → 1개
- 다운로드 용량·트래픽 약 1/3~1/5 수준

**상세 기획:** [AUTO_SUBS_SINGLE_LANG_PLAN.md](AUTO_SUBS_SINGLE_LANG_PLAN.md)

---

## 10. Skip 로직 정교화 및 Plist 3시·9시 (2026-01)

### 목적
- 라이브 종료 직후 VOD 처리 중인 영상이 잘못 Skip되는 문제 해결
- 멤버 전용 영상 즉시 Skip (재시도 낭비 방지)
- Plist 실행 시각: 9시 1회 → 3시·9시 2회

### 수정 (stt_function_v3.py)
- **extract_info 후**: `live_status in ("is_upcoming", "is_live")` → 사전 Skip (download 시도 없음)
- **DownloadError 처리**:
  - 멤버 전용 (`"members"` + `"level"`/`"only"`) → `__VIDEO_UNAVAILABLE__` 즉시 반환
  - `"live event"` 에러: `live_status`가 `was_live`/`post_live`/`not_live`이면 Skip 금지 → download_failed (다음 배치 재시도)
- Private/Video unavailable: 기존 유지

### 수정 (launchd plist)
- `StartCalendarInterval` 배열로 3:00, 9:00 두 시각 지정

### 수정 (docs/SCHEDULING.md)
- 3시·9시 실행 문서화

**상세 기획:** [SKIP_LOGIC_REFINEMENT_PLAN.md](SKIP_LOGIC_REFINEMENT_PLAN.md)

---

## 9. Cooling period 분기 검토 (2026-01, 결정만 문서화)

### 맥락
- 자막 전용 vs 음원 다운로드 시 cooling period를 분기할지 검토
- yt-dlp는 자막 다운로드 시 YouTube timedtext 엔드포인트 사용 (rate limit 엄격, 429 시 수 시간 IP 차단)

### 결정: 분기 없음, 현재 유지
- **30~40초 통일** 유지 (자막 전용/음원 다운로드 구분 없음)
- 이유: 분기 시 이득(비디오당 5~15초)은 작고, 429 리스크가 큼. 20~25초 안전성 실증 없음. 단순성 유지.

**코드 변경 없음.** 문서만 [RISK_ANALYSIS.md](RISK_ANALYSIS.md) 섹션 6, [PROJECT.md](PROJECT.md) Rate Limiting 참조에 반영.

---

## 11. Channel crawl: CID mismatch 해결 및 API 최적화 (2026-03)

### 해결한 이슈

**CID mismatch (@handle → 잘못된 채널 ID)**  
- @handle URL 해석 시 HTML에서 **첫 번째** channelId/externalId를 사용해 추천·관련 채널 ID가 선택되던 문제.  
- **조치**: canonical 링크 → canonicalBaseUrl → fallback 순으로 사용. `channel_df`에 **channel_id** 저장해 재해석 최소화.

**Redundant API 호출**  
- 매 run마다 `channels.list`(uploads_id 조회), @handle HTML 요청, playlistItems 전체 페이지네이션으로 Quota 소모.  
- **조치**: `channel_df`에 **channel_id**, **uploads_playlist_id** 캐시. 캐시 hit 시 channels.list·HTML 생략. **cursor_dt** 기준 playlistItems **조기 종료**.

**last_processed / last_discovered**  
- last_processed는 run 종료 후 `update_channel_last_processed_from_queue()`에서만 갱신(queue의 done 행 기준).  
- last_discovered는 `build_queue_and_get_candidates()` 내에서 API 결과의 최신 published_at으로 갱신.  
- crawl_yt_list에 잘못된 CID가 있어도, 신규 추가 행은 올바른 CID로 기록되며, 기존 잘못된 CID 행은 해당 채널의 last_processed 갱신에 사용되지 않음.

**uploads_playlist_id**  
- 채널당 **1개** (시스템 생성 uploads 플레이리스트). 여러 개가 아님.

### 수정 (channel_crawl.py)

- `CHANNEL_DF_COLUMNS`에 `channel_id`, `uploads_playlist_id` 추가.
- `load_channel_df()`: CSV에 유효한 UC/UU 형식이면 캐시 사용.
- `save_channel_df()`: channel_id, uploads_playlist_id 저장.
- `_resolve_handle_to_channel_id()`: CHANNEL_ID_CANONICAL_LINK_PATTERN, CHANNEL_ID_CANONICAL_BASEURL_PATTERN 우선 사용.
- `fetch_channel_via_api(api_key, channel_id, uploads_id=None, cursor_dt=None)`: uploads_id 캐시 시 channels.list 생략; cursor_dt 시 조기 종료; 반환 (entries, channel_title, uploads_id).
- `build_queue_and_get_candidates()`, `get_url_list_from_channel_crawl()`: 캐시 전달·반환값으로 채널 행 갱신.

### 문서

- [CHANNEL_CRAWL_API_USAGE.md](CHANNEL_CRAWL_API_USAGE.md): 해결된 이슈(CID mismatch, redundant API), last_processed/discovered, Quota, 최적화 적용 내용 정리.
- [PROJECT.md](PROJECT.md): channel_crawl 함수 설명에 channel_id/uploads_playlist_id 캐시 및 fetch_channel_via_api 반환값 반영.

---

## 12. YID/JSONL 리오그 및 MD 업로드 일자 헤더 (2026-01)

### 목적
- Obsidian MD에 **영상 업로드 일자** 메타데이터 추가
- JSONL 기반 메타데이터 중앙 관리 (live / offline 분리 후 merge)

### JSONL 구조
| 파일 | 역할 |
|------|------|
| `video_metadata_live.jsonl` | main.py 신규 배치 시 append |
| `video_metadata_offline.jsonl` | Phase 1/2 (과거 MD, YID-less 복구) |
| `video_metadata_merged.jsonl` | live + offline merge 결과 (md_add_upload_date_header 입력) |

### main.py 변경
- MD 저장 후 `video_metadata_live.jsonl`에 append
- `upload_date`가 있으면 MD **본문 최상단**에 `영상 업로드 일자: YYYY-MM-DD` 자동 추가

### 스크립트
- **`scripts/md_add_upload_date_header.py`**: `video_metadata_merged.jsonl` 기반으로 기존 MD에 업로드 일자 헤더 추가. 이미 있으면 스킵.
- **`scripts/build_jsonl_full.py`**: Phase 1 → cache → Phase 2 → merge 한 번에 실행. **초기/일회성용**. `--with-header`로 헤더 추가까지 포함 가능.

### 사용 시나리오
| 시나리오 | 필요한 작업 |
|----------|-------------|
| **신규 배치** | main.py만 실행. JSONL 저장 + MD 헤더 자동 추가됨 |
| **기존 MD에 헤더만 추가** | merged가 이미 있으면 `python scripts/md_add_upload_date_header.py` 한 번만 |
| **merged 최초 구축/재구축** | `python scripts/build_jsonl_full.py` |

**상세 기획:** [YID_JSONL_REORG_PLAN.md](YID_JSONL_REORG_PLAN.md)

---

## 13. 1KB 미만 summary 파일 재처리 (retry_small_summary_auto_subs)

### 목적
- summary 폴더 내 1KB 미만 파일은 input text가 너무 길어 token minimization/summary 단계에서 실패한 것으로 추정
- VID 추출 → auto subs 재다운로드 → chunk 기반 처리 → summary + MD 저장

### 스크립트
- **`scripts/retry_small_summary_auto_subs.py`**: 1KB 미만 .txt 스캔 → VID 추출 → auto subs 다운로드 → token_minimizer_chunked + summarize_with_chunking → **기존 summary 파일 덮어쓰기**, **기존 MD 경로 덮어쓰기** (없으면 신규)

### 사용법
```bash
python scripts/retry_small_summary_auto_subs.py [--base-path DIR] [--size-limit KB] [--dry-run]
# --size-limit 1 (default): 1KB 미만 파일만
# --dry-run: 대상 VID만 출력
```

---

## 수정된 파일 목록

| 파일 | 변경 내용 |
|------|-----------|
| config.py | SAVE_FULL_WHEN_AUTO_SUBS, FILENAME_MAX_LENGTH 추가 |
| main.py | base_name에 VID append, full 저장 조건 분기, auto_subs_only, md 날짜 폴더 직접 저장, video_metadata_live.jsonl append, MD 저장 시 upload_date 헤더 자동 추가 |
| vid_to_title_rename.py | VID-only batch rename, +vid- 스킵, 날짜 서브폴더 스캔 (`YYYY_MM_DD/`) |
| channel_crawl.py | auto_sub_only 컬럼, SKIPPED_AUTO_SUBS_ONLY_STATUS, queue reconcile/apply |
| stt_function_v3.py | yt_downloader_ytdlp auto_subs_only early-exit, _resolve_primary_lang, _yt_download_auto_subs_only 단일 언어 다운로드 |
| com.user.p03-md-relocate.plist | 신규 (주간 md_relocate) |
| docs/PROJECT.md | auto_sub_only, skipped_auto_subs_only 문서화 |
| docs/LOGIC_CHECK_MAIN.md | base_name 형식, full 저장 조건 |
| docs/OBSIDIAN_MD_RELOCATE.md | 날짜 폴더 직접 저장, md_relocate 레거시/plist 해제 |
| docs/SCHEDULING.md | md_relocate plist, use-case 테이블 |
| docs/UPDATE_LOG_20260128.md | 본 로그, auto_sub_only, 자동 자막 단일 언어 다운로드 |
| docs/AUTO_SUBS_SINGLE_LANG_PLAN.md | 자동 자막 단일 언어 다운로드 기획·구현 |
| docs/RISK_ANALYSIS.md | Cooling period 분기 검토·결정 (섹션 6) |
| docs/PROJECT.md | Rate Limiting 참조 (yt-dlp/timedtext, 분기 유지) |
| stt_function_v3.py | live_status 사전 Skip, 멤버 전용, post_live Skip 금지 |
| launchd/com.user.p03-speech2text.plist | StartCalendarInterval 3:00, 9:00 (당시; 이후 운영 변경은 아래 2026-05 메모·LAUNCHD.md) |
| docs/SCHEDULING.md | 3시·9시 실행 문서화 |
| docs/SKIP_LOGIC_REFINEMENT_PLAN.md | Skip 로직 정교화 기획·구현 완료 |
| channel_crawl.py | channel_id/uploads_playlist_id 캐시, canonical 기반 CID 해석, fetch_channel_via_api 조기 종료 |
| docs/CHANNEL_CRAWL_API_USAGE.md | 해결된 이슈(CID mismatch, redundant API), last_processed/discovered, 최적화 반영 |
| docs/PROJECT.md | channel_crawl channel_id/uploads_playlist_id, fetch_channel_via_api 반환값 |
| docs/UPDATE_LOG_20260128.md | 섹션 11 Channel crawl CID/API 최적화, 섹션 12 YID/JSONL 리오그 |
| docs/YID_JSONL_REORG_PLAN.md | 사용 시나리오별 요약 (6.1), build_jsonl_full 용도 명시 |
| docs/PROJECT.md | JSONL/MD 헤더 (프로젝트 구조, 데이터 플로우, 주요 개선 20번) |
| scripts/retry_small_summary_auto_subs.py | 신규: 1KB 미만 summary 재처리 (auto subs + chunk) |

---

## 운영 메모 (2026-05): launchd `78` / TCC / 로그 경로

- **실행:** plist의 `ProgramArguments`는 `~/Library/Application Support/com.user.p03-speech2text/run-p03-speech2text.sh` (원본 편집은 `Documents/Code/launchd/run-p03-speech2text.sh` → 수정 후 `cp`).
- **표준 출력·에러:** `~/Library/Logs/p03-speech2text/launchd_stdout.log` / `launchd_stderr.log` (예전 `p03_speech2text/logs/launchd_*.log` + `com.apple.macl` 조합으로 `78 EX_CONFIG` 나던 케이스 회피).
- **스케줄:** `StartCalendarInterval` 03:00, 09:00, 15:00; `RunAtLoad` true.
- 상세: [LAUNCHD.md](LAUNCHD.md), [`launchd/README.md`](../../../../launchd/README.md).
