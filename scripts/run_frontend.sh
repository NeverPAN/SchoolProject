#!/usr/bin/env bash
# 开发模式启动前端（Vite, 5173 端口）
set -e
cd "$(dirname "$0")/../frontend"
exec npm run dev "$@"
