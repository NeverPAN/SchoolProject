#!/usr/bin/env bash
# 启动 RQ 重建 Worker（单实例串行执行）
set -e
cd "$(dirname "$0")/../backend"
if [ -f .venv/bin/activate ]; then source .venv/bin/activate; fi
exec python -m app.workers.rq_worker
