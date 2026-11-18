#!/usr/bin/env bash
# 备份 SQLite；无参数时沿用应用配置并写入 DATA_DIR/backups。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="${RECON_PYTHON:-$ROOT/backend/.venv/bin/python}"
if [[ ! -x "$PYTHON" && -z "${RECON_PYTHON:-}" ]]; then PYTHON=python3; fi
exec "$PYTHON" -I "$ROOT/scripts/sqlite_backup.py" backup "$@"
