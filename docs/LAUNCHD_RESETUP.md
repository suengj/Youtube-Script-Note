# launchd p03-speech2text 다시 셋업 (Boot-out 5 나올 때)

상세·배경(`78 EX_CONFIG`, 로그 경로, 트러블슈팅)은 [SCHEDULING.md](SCHEDULING.md) §2 및 [LAUNCHD.md](LAUNCHD.md)를 참고하세요.

## 권장: install 스크립트 (2026-07+)

```bash
cd $PROJECT_ROOT
./scripts/install_launchd.sh
launchctl kickstart -k "gui/$(id -u)/com.user.p03-speech2text"
```

- plist 원본: `~/Developer/PJT/launchd/com.user.p03-speech2text.plist`
- 래퍼 배포: `~/Library/Application Support/com.user.p03-speech2text/run-p03-speech2text.sh`

## plist가 돌릴 때 하는 일

- **채널 크롤(스케줄 시각)** + **input_df.csv에 있는 URL**을 한 번에 처리합니다. (CHANNEL_CRAWL=true일 때 채널 후보와 input_df를 병합해 돌리도록 되어 있음.)

## conda 환경

- **터미널에서 launchctl 명령만 쓸 때 (bootout, bootstrap, kickstart):** conda **base**든 **ai**든 상관없음.
- **실제 스케줄에 main.py가 실행될 때:** 래퍼가 **`.../envs/ai/bin/python`** 으로 `exec` → **항상 conda ai 환경**.

## 지금 한 번 강제 실행 (kickstart)

```bash
launchctl kickstart -k gui/$(id -u)/com.user.p03-speech2text
```

---

## 수동 재등록 (install_launchd.sh 실패 시)

```bash
P03=$PROJECT_ROOT
PLIST_SRC=~/Developer/PJT/launchd/com.user.p03-speech2text.plist
launchctl bootout gui/$(id -u)/com.user.p03-speech2text 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.user.p03-speech2text.plist
ln "$PLIST_SRC" ~/Library/LaunchAgents/com.user.p03-speech2text.plist
mkdir -p "$HOME/Library/Application Support/com.user.p03-speech2text" "$HOME/Library/Logs/p03-speech2text" "$P03/logs"
cp -f ~/Developer/PJT/launchd/run-p03-speech2text.sh \
  "$HOME/Library/Application Support/com.user.p03-speech2text/run-p03-speech2text.sh"
chmod +x "$HOME/Library/Application Support/com.user.p03-speech2text/run-p03-speech2text.sh"
plutil -lint ~/Library/LaunchAgents/com.user.p03-speech2text.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.p03-speech2text.plist
launchctl list | grep p03-speech2text
```

- **실행 시각:** 매일 **03:00, 09:00, 15:00** (맥 로컬 시간).
