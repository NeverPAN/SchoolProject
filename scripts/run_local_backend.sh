#!/usr/bin/env bash
# 启动 FastAPI 后端（默认 8000 端口）
set -e
cd "$(dirname "$0")/../backend"
if [ -f .venv/bin/activate ]; then source .venv/bin/activate; fi
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${BACKEND_PORT:-8000}" "$@"
