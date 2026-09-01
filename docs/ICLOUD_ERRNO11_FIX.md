# iCloud 경로에서 OSError [Errno 11] Resource deadlock avoided 해결

프로젝트가 **iCloud Drive** (`Library/Mobile Documents/com~apple~CloudDocs/...`) 안에 있으면, 파일 읽기/쓰기 시 iCloud 동기화가 해당 파일을 잠그는 타이밍에 **OSError [Errno 11] Resource deadlock avoided** 가 발생할 수 있다.

---

## 1. 원인

- iCloud가 동기화·잠금 중인 파일을 스크립트가 동시에 열거나 수정하려 할 때 발생.
- `pd.read_csv()`, `open()`, `load_dotenv()`, `df.to_csv()` 등 **로컬 파일 I/O**가 iCloud 경로를 참조하면 어디서든 발생 가능.

---

## 2. 해결 패턴 (다른 프로젝트에도 적용)

### (1) 읽기: 재시도 + sleep

Errno 11이면 2~3회 재시도, 재시도 사이에 2~4초 sleep.

```python
import time

def read_csv_with_retry(path: str, encoding: str = "utf-8-sig", max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            return pd.read_csv(path, encoding=encoding)
        except OSError as e:
            if getattr(e, "errno", None) == 11 and attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
```

- `open()` / `load_dotenv()` 등도 같은 방식: `try` → `OSError`, `errno == 11`이면 `time.sleep` 후 재시도.

### (2) 쓰기: 임시 파일 + os.replace

원본 파일에 직접 쓰지 않고, 임시 파일에 쓴 뒤 한 번에 교체해 잠금 구간을 줄인다.

```python
tmp_path = path + ".tmp"
df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
os.replace(tmp_path, path)
```

- 여전히 Errno 11이 나면 (2)도 재시도 루프로 감싼다.

### (3) 근본 대응

- **iCloud 폴더 안에 둔 경우**: Finder에서 해당 폴더(프로젝트 또는 데이터 경로) 우클릭 → **"다운로드"** / **"항상 이 Mac에 보관"** 으로 강제 다운로드해 두면, 파일 읽기/쓰기 시 동기화 잠금으로 인한 Errno 11을 막을 수 있다.
- 가능하면 **프로젝트 또는 자주 쓰는 데이터/설정 파일을 iCloud 동기화 폴더 밖**으로 두기 (로컬 전용 경로).
- **다운로드만 iCloud 밖으로 (권장):** `.env`에 **`WORK_PATH`**를 로컬 경로로 설정하면, 오디오 다운로드(`audio/`)와 자막 다운로드(`yt_subs/`)만 해당 경로에 저장됩니다. 스트리밍 쓰기(.part 등)가 iCloud 동기화를 거치지 않아 deadlock 가능성이 사라집니다. CSV·로그·output_new·마크다운은 기존처럼 `BASE_PATH`(iCloud)를 사용합니다.
  - 예: `WORK_PATH=$WORK_PATH` (iCloud가 아닌 로컬 폴더)
  - 설정하지 않으면 `WORK_PATH` 없이 `BASE_PATH`만 사용(기존 동작).

---

## 3. 다른 프로젝트용 해결 프롬프트 (AI에 붙여넣기)

```
이 프로젝트가 iCloud Drive(Library/Mobile Documents/com~apple~CloudDocs) 안에 있어서, .env나 CSV 등 로컬 파일을 읽을 때 iCloud가 동기화하면서 파일을 잠그면 OSError [Errno 11] Resource deadlock avoided 가 난다. 해결해줘.

해결 방법: (1) 해당 파일을 읽거나 쓸 때 Errno 11이면 최대 2~3번 재시도하고, 재시도 사이에 2~4초 sleep 넣기. (2) 쓰기 시에는 임시 파일에 쓴 뒤 os.replace(tmp, path)로 한 번에 교체해서 잠금 시간을 줄이기. (3) 가능하면 프로젝트나 자주 쓰는 데이터 파일을 iCloud 동기화 폴더 밖으로 옮기기.
```

---

## 4. 이 프로젝트(p03_speech2text)에서 적용한 곳

| 위치 | 조치 |
|------|------|
| `stt_function_v3.py` | `load_dotenv()` — Errno 11 시 재시도 |
| `main.py` | `_read_csv_with_retry()` 추가, `load_output_df_only`, `load_dataframes`, `get_input_urls_for_channel_crawl` 에서 CSV 읽기 시 사용 |
| `channel_crawl.py` | `save_crawl_queue_df` — 임시 파일 + `os.replace` + 재시도. `load_crawl_queue_df`, `update_channel_last_processed_from_queue` 내부 `read_csv` — 재시도 루프 |
| `stt_function_v3.py` | **yt-dlp 다운로드** (오디오/비디오 파일 쓰기) — DownloadError 메시지에 `errno 11` 또는 `resource deadlock` 포함 시 최대 **6회** 재시도, 재시도 전 **12~25초** 대기, 로그에 **"Local I/O contention (Errno 11, often EDEADLK on macOS)…"** 기록 |
| `stt_function_v3.py` | **자막만 다운로드** (`_yt_download_subs_only`, `_yt_download_auto_subs_only`) — **`nopart: True`** 로 `.vtt.part` 단계 완화. Errno 11 시 최대 **3회** 재시도, 재시도 전 **5~12초** 대기·`.part` 정리. 실패 시 비디오+Whisper 경로로 폴백 |
| `config.py` / `main.py` | 시작 시 `log_macos_deadlock_path_warnings` — `DATA_ROOT`·`WORK_PATH`·`BASE_PATH`·`TMPDIR`·`XDG_CACHE_HOME` 이 iCloud/CloudStorage 경로면 **경고 로그** |
| `main.py` | launchd/cron 실행 시 **tqdm**이 stdout(→ iCloud 로그 파일)에 flush 하다 Errno 11 → stdout이 TTY가 아닐 때 tqdm 출력을 `os.devnull`로 분기 |
| `main.py` | **로깅**이 stdout에 flush 하다 Errno 11 → stdout이 TTY가 아닐 때 `StreamHandler(sys.stdout)` 미추가, FileHandler만 사용 |

**참고 (파일 I/O는 강제 다운로드로 배제했다고 가정):**  
그래도 파일 접근에서 11이 나면 `main.py`의 `output_df.to_csv(...)`, `run_lock.py` lock, `stt_function_v3.py`/기타 `open()` 등에 재시도·임시파일 패턴을 적용하면 된다. **실제로 남는 Errno 11 가능 구간은 4.1** 참고.

---

## 4.1 남은 Errno 11 상황 (파일 I/O는 강제 다운로드로 배제한 뒤)

**가정:** 프로젝트/데이터 폴더를 iCloud에서 **항상 이 Mac에 보관**해 두어서, 일반적인 파일 읽기·쓰기(CSV, .env, lock 등)로 인한 Errno 11은 발생하지 않는다고 본다.

**그래서 남는 건** launchd/cron이 **stdout·stderr를 파일**로 리다이렉트하는 경우뿐이다. (p03 STT는 plist에서 **`~/Library/Logs/p03-speech2text/launchd_*.log`** 로 두어 iCloud·`macl` 리스크를 줄였다.) 그 파일은 실행 중 계속 쓰이는 스트림이라, 강제 다운로드와 무관하게 flush 시 iCloud 쪽에서 잠금이 걸리면 Errno 11이 날 수 있다.

| 남은 상황 | 설명 | 대응 |
|-----------|------|------|
| **tqdm** | progress bar가 stdout에 flush → 리다이렉트된 로그 파일에서 11 가능 | ✅ **이미 처리:** stdout이 TTY가 아닐 때(`isatty()` + launchd/cron env) tqdm 출력을 `os.devnull`로 보냄 |
| **로깅** | `main.py`의 `StreamHandler(sys.stdout)` → `logger.info()` 등이 같은 stdout(로그 파일)에 flush | ✅ **적용함:** stdout이 TTY가 아닐 때(`isatty()`) StreamHandler를 추가하지 않음. FileHandler만 사용 → `logs/stt_YYYYMMDD.log`에만 기록 |
| **stt_function_v3.py의 print()** | 전사 시작/완료, FFmpeg/Whisper 에러 등 `print()` → 동일하게 stdout(로그 파일) flush | 발생 시 해당 print를 logger로 바꾸거나, 스케줄 시 print 최소화 |

---

## 5. Errno 11 영향 받는 파일 리스트 (p03_speech2text)

### 5.1 대상 파일 (경로가 iCloud 안이면 영향 받을 수 있음)

| 대상 파일 | 경로 기준 | 비고 |
|-----------|-----------|------|
| `.env` | 프로젝트 루트 (BASE_PATH) | load_dotenv |
| `input_df.csv` | base_path | URL 목록 |
| `output_df_new.csv` | base_path | 처리 결과 추적 |
| `crawl_yt_list.csv` | base_path | 채널 크롤 큐 |
| `channel_df.csv` | base_path | 채널 목록·last_processed |
| `output_new/summary/*.txt` | base_path | 간소화 전사 저장 |
| `output_new/full/*.txt` | base_path | 전체 전사 저장 |
| `failed_urls.txt` | base_path | 실패 URL 목록 |
| `yt_subs/*.vtt`, `*.srt` | base_path | 업로더 자막 |
| Lock 메타/락 파일 | base_path (run_lock) | 중복 실행 방지 |
| launchd stdout/stderr (plist) | **`~/Library/Logs/p03-speech2text/launchd_*.log`** (권장) | 예전처럼 `base_path/logs/launchd_*.log` 를 쓰면 iCloud·`com.apple.macl` 조합으로 launchd **`78 EX_CONFIG`** 가 날 수 있음. tqdm flush 시 Errno 11 가능성은 여전히 “표준 출력이 파일인 경우”에 해당 |
| `OUTPUT_MD_PATH` 아래 `*.md` | .env 설정 | 마크다운 출력 (Obsidian 등 iCloud일 수 있음) |
| `prompt/logs/*` | base_path | 프롬프트 로그 |

### 5.2 소스 코드별 I/O 위치 및 재시도 적용 여부

| 소스 파일 | 대상 | 동작 | 라인 | 재시도/대응 |
|-----------|------|------|------|-------------|
| **main.py** | .env | 읽기 | 35 (load_dotenv) | main은 stt import 전이라 stt의 load_dotenv가 먼저 실행 → stt에서 재시도 적용 |
| **main.py** | input_df.csv | 읽기 | 193, 309 | ✅ _read_csv_with_retry |
| **main.py** | output_df_new.csv | 읽기 | 219, 280 | ✅ _read_csv_with_retry |
| **main.py** | output_df_new.csv | 쓰기 | _save_output_df_with_retry 사용처 전부 | ✅ 임시파일 + os.replace + Errno 11 재시도 |
| **main.py** | tqdm (stdout flush) | 쓰기 | process_videos tqdm | ✅ launchd/cron 시 file=devnull |
| **main.py** | concise (summary) | 쓰기 | 582 open | ❌ 미적용 |
| **main.py** | md 파일 (`OUTPUT_MD_PATH`) | 쓰기 | `atomic_write_text_with_retry` | ✅ 동일 폴더 `.tmp` + `os.replace` + Errno 11 재시도; **primary 실패 시에만** `WORK_PATH/output_md_mirror/` 폴백 |
| **main.py** | failed_urls.txt | 쓰기 | 1038 open | ❌ 미적용 |
| **channel_crawl.py** | .env | - | - | 사용 안 함 (main/stt에서 로드) |
| **channel_crawl.py** | channel_df.csv | 읽기/쓰기 | 164 open r, 205 open w | ❌ 미적용 |
| **channel_crawl.py** | crawl_yt_list.csv | 읽기 | 278 read_csv | ✅ 재시도 루프 |
| **channel_crawl.py** | crawl_yt_list.csv | 쓰기 | 309 to_csv+replace | ✅ 임시파일+재시도 |
| **channel_crawl.py** | output_df_new.csv | 읽기 | 866 read_csv | ✅ 재시도 루프 |
| **stt_function_v3.py** | .env | 읽기 | 34 load_dotenv | ✅ 재시도 |
| **stt_function_v3.py** | yt-dlp 다운로드 (오디오/비디오 쓰기) | 쓰기 | yt_downloader_ytdlp | ✅ Errno 11 시 최대 6회 재시도, 12~25초 대기 (로그는 EDEADLK/로컬 I/O 중심 문구) |
| **stt_function_v3.py** | 자막만 다운로드 (yt_subs/*.vtt 등 쓰기) | 쓰기 | _yt_download_subs_only / _yt_download_auto_subs_only | ✅ `nopart: True` + Errno 11 시 최대 3회 재시도, 5~12초 대기 |
| **stt_function_v3.py** | 자막 파일 (path) | 읽기 | 325 open | ❌ 미적용 |
| **stt_function_v3.py** | output.wav (Whisper용 임시) | 쓰기 | transcribe_by_mlx | ✅ WORK_PATH 설정 시 temp_path(로컬)에 저장 |
| **stt_function_v3.py** | 전사 저장 파일 | 쓰기 | 1042, 1090 open | ❌ 미적용 |
| **stt_function_v3.py** | prompt log (`prompt/logs/prompt_log.json`) | 읽기/쓰기 | `prompt_log()` | ✅ `WORK_PATH` 설정 시 `WORK_PATH/prompt/logs`에 저장 + 임시파일 + `os.replace` + Errno 11 재시도 |
| **run_lock.py** | lock 메타 | 읽기/쓰기 | 50 open r, 57 open w | ❌ 미적용 |
| **run_lock.py** | lock 파일 | 열기 | 89 open | ❌ 미적용 |

---

## 6. macOS 자막/다운로드 Errno 11 완화 체크리스트 (운영)

아래는 **ko-orig / en-orig 등 트랙 이름**과 무관하게, **로컬 I/O·동기화·백그라운드**에서 같은 증상이 날 때 점검할 항목이다.

| 항목 | 조치 |
|------|------|
| **경로** | `DATA_ROOT`, `WORK_PATH`, `TMPDIR`, `XDG_CACHE_HOME` 이 **iCloud Drive / Mobile Documents / Library/CloudStorage**(원드라이브·드롭박스 등) 아래가 아닌지 확인. 시작 시 `config.log_macos_deadlock_path_warnings` 가 위험 경로면 WARN 로그. |
| **iCloud “최적화 저장”** | 자료가 **클라우드만 있고 로컬에 없음(evicted)** 이면, 터미널과 달리 **launchd** 백그라운드에서 접근이 꼬일 수 있음. 해당 폴더는 Finder에서 **다운로드 / 이 Mac에 항상 유지** 하거나, 데이터 루트를 **비동기화 로컬 폴더**(`$PROJECT_ROOT/data`)로 둔다. |
| **launchd** | `com.user.p03-speech2text.plist`: `WORK_PATH`, `TMPDIR`, `XDG_CACHE_HOME` 이 **같은 프로젝트 루트**로 맞는지 확인. 설치: `./scripts/install_launchd.sh`. 실행: **`~/Library/Application Support/.../run-p03-speech2text.sh`**, 로그: **`~/Library/Logs/p03-speech2text/`**. 상세: [SCHEDULING.md](SCHEDULING.md), [LAUNCHD.md](LAUNCHD.md). |
| **단일 실행** | `run_lock.acquire_run_lock` + 스케줄과 수동 실행 충돌 방지. 같은 영상·같은 출력에 **두 프로세스가 동시에 쓰지 않게** 한다. |
| **yt-dlp** | `python -m pip install -U yt-dlp` (conda `ai` 환경)로 주기적 업데이트. |
| **자막 `.part`** | `_yt_download_subs_only` / `_yt_download_auto_subs_only` 에 **`nopart: True`** 적용됨. |
| **백신/실시간 검사(선택)** | 서드파티 백신이 `.part`/다운로드 파일을 스캔하며 잠그면 rename 단계에서 Errno 11이 날 수 있음. `WORK_PATH`·`TMPDIR`·`XDG_CACHE_HOME` 트리를 **실시간 검사 제외**에 넣고 동일 영상으로 재시도. |
