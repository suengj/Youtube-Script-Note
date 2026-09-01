# launchd (p03_speech2text)

plist 원본: **`~/Developer/PJT/launchd/com.user.p03-speech2text.plist`**.  
설치: **`scripts/install_launchd.sh`** (Application Support 래퍼 복사 + LaunchAgents 하드 링크 + bootstrap).

## 동작 요약

| 항목 | 내용 |
|------|------|
| Label | `com.user.p03-speech2text` |
| 스케줄 | 매일 **03:00**, **09:00**, **15:00** (로컬) |
| `WORK_PATH` | `$PROJECT_ROOT` (통합 루트) |
| `TMPDIR` / `XDG_CACHE_HOME` | `{WORK_PATH}/tmp`, `{WORK_PATH}/cache` |
| 실행 | `~/Library/Application Support/com.user.p03-speech2text/run-p03-speech2text.sh` |
| 로그(메인) | `logs/stt_YYYYMMDD.log` |
| launchd I/O | `~/Library/Logs/p03-speech2text/launchd_*.log` |

## 설치 / 재등록

```bash
cd $PROJECT_ROOT
chmod +x scripts/install_launchd.sh
./scripts/install_launchd.sh
```

## 수동 한 번 실행

```bash
launchctl kickstart -k "gui/$(id -u)/com.user.p03-speech2text"
```

`Could not find service` 이면 `./scripts/install_launchd.sh` 로 먼저 등록하세요.

## 트러블슈팅 (일괄 `download_failed`)

`logs/stt_YYYYMMDD.log` 에 **`[Errno 32] Broken pipe`** 가 연속으로 찍히면, 영상 하나 문제가 아니라 **yt-dlp → ffmpeg 파이프/네트워크** 쪽 이슈입니다. 대응: `pip install -U yt-dlp`, ffmpeg 재설치·경로 확인, `WORK_PATH` 디스크 여유, VPN/프록시 안정성, 필요 시 `.env` 의 쿠키 파일(`YOUTUBE_COOKIES_FILE`). 같은 URL을 터미널에서 `yt-dlp -v 'URL'` 로 한 번 재현해 보세요.

## `78 EX_CONFIG` / plist는 있는데 안 뜸

`launchctl list`에 **PID 없이 `78`**만 보이면 **설정·경로 단계에서 launchd가 기동 실패**한 경우가 많습니다. 실행 경로가 Application Support 래퍼인지, 로그가 `~/Library/Logs/p03-speech2text/` 인지 확인하세요. plist·래퍼 수정 후 `./scripts/install_launchd.sh` 로 재등록.

## plist / 래퍼 수정 후

`./scripts/install_launchd.sh` (bootout → 하드 링크 → bootstrap). LaunchAgents plist가 **하드 링크**인 경우 일부 에디터 저장 시 링크가 끊길 수 있으니 install 스크립트로 다시 링크하세요.

## iCloud DATA 미러 (retired 2026-07)

`com.user.p03-data-mirror-icloud` — **사용 중단**. `scripts/mirror_data_root_to_icloud.py`는 기본 비활성(`P03_DISABLE_ICLOUD_MIRROR=1`).

마이그레이션: [MIGRATION_20260711.md](MIGRATION_20260711.md)
