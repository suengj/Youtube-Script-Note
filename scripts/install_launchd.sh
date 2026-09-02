#!/usr/bin/env bash
# Install com.user.p03-speech2text launchd job (signal_guide pattern).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCHD_SRC="${P03_LAUNCHD_SRC:-${PROJECT}/launchd}"
LABEL="com.user.p03-speech2text"
APP_SUPPORT="${HOME}/Library/Application Support/com.user.p03-speech2text"
LOGS="${HOME}/Library/Logs/p03-speech2text"
PLIST_SRC="${LAUNCHD_SRC}/com.user.p03-speech2text.plist"
WRAPPER_SRC="${LAUNCHD_SRC}/run-p03-speech2text.sh"
[[ -f "$PLIST_SRC" ]] || PLIST_SRC="${LAUNCHD_SRC}/com.user.p03-speech2text.plist.example"
[[ -f "$WRAPPER_SRC" ]] || WRAPPER_SRC="${LAUNCHD_SRC}/run-p03-speech2text.sh.example"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -h | --help)
      echo "Usage: $(basename "$0") [--dry-run]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

for f in "$PLIST_SRC" "$WRAPPER_SRC"; do
  if [[ ! -f "$f" ]]; then
    echo "Missing launchd source: $f (copy launchd/*.example and edit paths)" >&2
    exit 1
  fi
done
if [[ "$PLIST_SRC" == *.example || "$WRAPPER_SRC" == *.example ]]; then
  echo "NOTE: installing from .example files — edit paths before relying on scheduled runs." >&2
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "DRY RUN"
  echo "  PROJECT              = ${PROJECT}"
  echo "  LAUNCHD_SRC           = ${LAUNCHD_SRC}"
  echo "  APP_SUPPORT wrapper   = ${APP_SUPPORT}/run-p03-speech2text.sh"
  echo "  PLIST_DST             = ${PLIST_DST}"
  echo "  LOGS                  = ${LOGS}/"
  exit 0
fi

mkdir -p "$APP_SUPPORT" "$LOGS" "${PROJECT}/logs"
cp -f "$WRAPPER_SRC" "${APP_SUPPORT}/run-p03-speech2text.sh"
chmod +x "${APP_SUPPORT}/run-p03-speech2text.sh"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
sleep 1
rm -f "$PLIST_DST"
ln "${PLIST_SRC}" "$PLIST_DST"
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "==> Installed ${LABEL}"
echo "    Schedule: see StartCalendarInterval in ${PLIST_SRC}"
echo "    Logs: ${LOGS}/"
echo "    Test: launchctl kickstart -k gui/$(id -u)/${LABEL}"
