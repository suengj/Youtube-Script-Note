#!/bin/bash
# 1회 실행용: retry_small_summary_auto_subs 실행 후 launchd plist 자동 해제
# launchd에서 호출됨 (com.user.p03-retry-small-summary-once.plist)

set -e
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-/opt/homebrew/Caskroom/miniforge/base/envs/ai/bin/python}"
LOG_DIR="$(pwd)/logs"
mkdir -p "$LOG_DIR"

"$PYTHON" scripts/retry_small_summary_auto_subs.py >> "$LOG_DIR/retry_small_summary.log" 2>&1

# 1회만 사용: 실행 후 plist unload
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.user.p03-retry-small-summary-once.plist 2>/dev/null || true
