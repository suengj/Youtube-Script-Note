#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export BASE_PATH="${BASE_PATH:-$HOME/p03-run}"
export WORK_PATH="${WORK_PATH:-$HOME/p03-run}"
export DATA_ROOT="${DATA_ROOT:-$HOME/p03-run/data}"
export SYSTEMD_SCHEDULED=1
cd "$ROOT"
exec "${P03_PYTHON:-$HOME/.venvs/p03/bin/python}" main.py "$@"
