# YID/JSONL Reorg Plan — Obsidian MD 메타데이터 정리 기획서

## 1. 목표

- 대화창에서 나온 기획 내용을 `.md`로 정리하여 **버전 관리(Git) 가능**, **검색·참조 용이**, **이후 업데이트 시 문제 없이 수정 가능**하게 함
- Obsidian MD 파일에 **영상 업로드 일자** 메타데이터 추가 및 YID-less 파일 복구 지원

---

## 2. 설계 결정 요약

| 항목 | 결정 |
|------|------|
| **파일 경로** | **유지** — 기존 `YYYY_MM_DD/` 폴더 구조 및 파일 경로 변경 없음 |
| **업로드 일자 표시** | MD 파일 **본문 최상단**에 `영상 업로드 일자: YYYY-MM-DD` 추가 (파일 경로 재편 아님) |
| **JSONL 역할** | 전사일자·업로드일자·YID 등 메타데이터 중앙 관리, 향후 파일명 일괄 변경 시 참조 |
| **output_df 날짜 이슈** | output_df 날짜 기반 매칭 불안정 → **Title Cache**로 대체 |
| **JSONL 메타데이터** | CSV에 병합하지 않고 **별도 JSONL 파일**로 관리 |
| **JSONL 분리** | live(main.py) / offline(복구 스크립트) 동시 쓰기 충돌 방지를 위해 **분리 후 merge** |
| **YID-less 표시** | JSONL에 `has_yid=false`, `method="no_yid"`로 기록, mapping/indexing 고려 |

### 2.1 변경 요청 배경 (파일 경로 유지)

- **기존 요청**: JSONL 구축 후 파일 경로를 영상 업로드 날짜로 재편
- **변경 요청**: 
  - JSONL 구축은 동일하나 **기존 파일 경로 유지**
  - `영상 업로드 일자: YYYY-MM-DD`를 MD **본문 최상단**에 기입
  - **이유**: JSONL에 전사일자·업로드일자 등이 이미/향후 기입되므로 지속 관리 가능 (YID와 함께)
  - MD는 현재 방식 유지, 나중에 파일명 일괄 변경이 필요하면 **JSONL 참고**하여 진행 가능
  - YID-less 파일은 JSONL에 별도 표기 필요 → **mapping/indexing** 방법 고려

---

## 3. YID 기반 Mapping / Indexing

| 소스 | YID(v_id) | 용도 |
|------|-----------|------|
| **JSONL** | `v_id`, `has_yid` | 메타데이터 중앙 저장, MD·Summary·파일 경로 매핑 기준 |
| **Obsidian MD** | 파일명/경로에 VID 포함 또는 YID-less | MD 본문 + 경로 |
| **output_new/summary/** | 파일명에 VID 포함 또는 YID-less | 전사 요약 텍스트 |
| **vid_title_cache.json** | v_id → title | YID-less 복구용 Title Cache |

- **YID-less 처리**: JSONL에 `has_yid=false`, `method="no_yid"`로 기록
- **Caching**: Title Cache로 YID-less 파일의 v_id 복구 시도, 실패 시 JSONL에 별도 표기
- **향후 파일명 일괄 변경**: JSONL의 v_id·md_path·upload_date를 참고하여 스크립트로 수행 가능

---

## 4. 완료된 항목

### 4.1 Option B: 2-step summarization

- **목적**: 긴 VTT 입력이 272k 토큰 제한 초과 시 처리
- **구현**: `stt_function_v3.py`, `main.py`
  - `count_tokens()`, `chunk_text_by_tokens()`
  - `INPUT_TOKEN_LIMIT=200000`, `CHUNK_TOKEN_SIZE=150000`
  - `token_minimizer_chunked()`, `summarize_with_chunking()` (chunk → summarize → merge)
- **결과**: main.py에서 입력 토큰 초과 시 자동으로 청킹 후 요약 병합

### 4.2 JSONL metadata schema 및 write 로직 (main.py 구축)

- **파일**: `stt_function_v3.py`, `main.py`
- **함수**: `append_video_metadata_jsonl(jsonl_path, upload_date, v_id, transcript_date, method, md_path)`
- **스키마**: `{upload_date, v_id, transcript_date, method, md_path}`
  - `method`: `"whisper"` | `"subs"` | `"auto_subs"`
- **경로**: `yt_downloader` 반환값에 `upload_date` 포함, `_format_upload_date()`로 `YYYY-MM-DD` 포맷
- **호출**: main.py에서 MD 저장 후 `video_metadata_live.jsonl`에 append (신규 배치용)
- **has_yid**: 스키마에 `has_yid` (bool) 추가, live는 항상 True

### 4.3 Pre-check script (`scripts/yid_precheck.py`)

- **기능**:
  - Obsidian MD 스캔, YID 있음/없음 파일 수 집계
  - YID-less 파일의 recoverability 추정 (Title Cache 또는 live fetch)
- **인코딩 처리**: Unicode NFC, 전각 공백, trailing Hangul jamo, `.m4a+vid-XXX` prefix 추출
- **날짜 정규화**: output_df 날짜 `2025.1.18` → `YYYY-MM-DD`
- **Fallback**: exact date → same month → success v_ids 첫 200개
- **tqdm**: `--build-cache` 시 진행률 표시

### 4.4 Title Cache mode (YouTube Data API)

- **`--build-cache`**: output_df success v_ids로 `vid_title_cache.json` 구축 — **YouTube Data API** 50개 단위 배치 (CPU/네트워크 부하 최소화)
- **`--use-cache`**: YID-less 파일을 캐시와 매칭 (live fetch 없음)
- **YOUTUBE_API_KEY** 필요 (.env)

### 4.5 Backup

- `backup/2026-03-16-backup.zip`에 `.py` 파일 백업 완료

---

## 5. 상세 To-Do List

### 5.1 JSONL split

| To-Do | 상태 | 설명 |
|-------|------|------|
| main.py 출력 경로 변경 | **완료** | `video_metadata_live.jsonl` (신규 배치) |
| offline JSONL 경로 | **완료** | `video_metadata_offline.jsonl` (Phase 1/2) |
| merge 결과 경로 | **완료** | `video_metadata_merged.jsonl` |

### 5.2 has_yid 필드

| To-Do | 상태 | 설명 |
|-------|------|------|
| JSONL 스키마 확장 | **완료** | `has_yid` (bool) 추가 |
| YID-less 기록 | **완료** | `has_yid=false`, `method="no_yid"` |

### 5.3 Merge script

| To-Do | 상태 | 설명 |
|-------|------|------|
| `scripts/video_metadata_merge.py` | **완료** | live + offline → merged, md_path dedupe, legacy video_metadata.jsonl 지원 |

### 5.4 MD header insertion

| To-Do | 상태 | 설명 |
|-------|------|------|
| `scripts/md_add_upload_date_header.py` | **완료** | `video_metadata_merged.jsonl` 기반, MD **본문 최상단**에 `영상 업로드 일자: YYYY-MM-DD` 추가 |
| 기존 헤더 처리 | **완료** | 이미 있으면 스킵 |

### 5.5 .env rate limiting (Title Cache용)

| To-Do | 상태 | 설명 |
|-------|------|------|
| 환경변수 추가 | 미완료 | `YID_CACHE_MIN_DELAY=2`, `YID_CACHE_MAX_DELAY=4`, `YID_CACHE_EXTENDED_INTERVAL=40`, `YID_CACHE_EXTENDED_DURATION=60` |

### 5.6 Offline JSONL phase 1/2

| To-Do | 상태 | 설명 |
|-------|------|------|
| Phase 1 스크립트 | **완료** | `scripts/md_to_offline_jsonl_phase1.py` — YID 있는 MD → offline JSONL |
| Phase 2 확장 | **완료** | `yid_precheck.py --use-cache --write-offline-jsonl --sample 0` |

---

## 6. 실행 순서 (권장)

### 6.1 사용 시나리오별 요약

| 시나리오 | 필요한 작업 |
|----------|-------------|
| **신규 배치** (main.py 실행) | 별도 스크립트 없음. main.py가 `video_metadata_live.jsonl`에 저장 + MD 저장 시 본문 상단에 업로드 일자 자동 추가 |
| **기존 MD에 헤더만 추가** | `video_metadata_merged.jsonl`이 이미 있으면 → `python scripts/md_add_upload_date_header.py` 한 번만 실행 |
| **merged JSONL 최초 구축/재구축** | `python scripts/build_jsonl_full.py` (또는 `--with-header`로 헤더까지) |

**build_jsonl_full**은 다음 경우에만 필요함:
- 처음으로 merged JSONL을 만들 때
- 오프라인 MD를 새로 추가했을 때 (Phase 1/2로 offline JSONL 재구축)
- merged를 갱신해야 할 때

---

### 6.2 전체 파이프라인 (초기/일회성)

**한 번에 실행:**
```bash
python scripts/build_jsonl_full.py
# 또는 헤더까지: python scripts/build_jsonl_full.py --with-header
```

**단계별:**
1. **Phase 1**: `python scripts/md_to_offline_jsonl_phase1.py` — YID 있는 MD → offline JSONL
2. **Title Cache**: `python scripts/yid_precheck.py --build-cache`
3. **Phase 2**: `python scripts/yid_precheck.py --use-cache --write-offline-jsonl --sample 0`
4. **Merge**: `python scripts/video_metadata_merge.py` — live + offline → merged
5. **MD header insertion**: `python scripts/md_add_upload_date_header.py` — 과거 MD 일괄 업데이트
6. **신규 배치**: main.py는 `video_metadata_live.jsonl`에 append + **MD 저장 시 본문 상단에 업로드 일자 자동 추가**

---

## 7. 관련 파일

| 파일 | 역할 |
|------|------|
| `main.py` | 메인 파이프라인, `video_metadata_live.jsonl` append (신규 배치) |
| `stt_function_v3.py` | `append_video_metadata_jsonl` (has_yid), `_format_upload_date` |
| `scripts/yid_precheck.py` | YID 카운트, Title Cache, `--write-offline-jsonl` (Phase 2) |
| `scripts/md_to_offline_jsonl_phase1.py` | 과거 영상 MD → `video_metadata_offline.jsonl` |
| `scripts/video_metadata_merge.py` | live + offline → `video_metadata_merged.jsonl` |
| `scripts/build_jsonl_full.py` | 1–4단계 한 번에 실행 (Phase1 → cache → Phase2 → merge). **초기/일회성용**. 일상 배치는 main.py만 사용 |
| `scripts/md_add_upload_date_header.py` | 과거 MD 본문 상단에 `영상 업로드 일자` 추가 |
| `scripts/retry_small_summary_auto_subs.py` | 1KB 미만 summary → VID 추출 → auto subs 재다운로드 → chunk 처리 → summary/MD 저장 |
| `vid_to_title_rename.py` | VID 추출, `extract_vid_from_md_name`, `has_extractable_vid` |
| `output_df_new.csv` | 처리 이력 (Title Cache v_id 소스) |
| `vid_title_cache.json` | Title Cache (v_id → title) |
| `docs/YOUTUBE_API_SETUP.md` | YouTube API 설정 |

---

## 8. 참고: +vid- 패턴

- **Whisper 형식**: `{title}.m4a+vid-{VID}_*.md` — VID가 11자로 명시됨
- **YID-less**: `+vid-` 뒤가 비어 있거나 11자가 아님
- **has_extractable_vid()**: `+vid-VID` (11자) 패턴이 있으면 YID 있음으로 간주
