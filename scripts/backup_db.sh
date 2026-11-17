#!/usr/bin/env bash
# 备份 SQLite 数据库到数据目录（建议 cron 定期执行）
set -e
ENV_FILE="$(dirname "$0")/../.env"
DB=$(grep '^DATABASE_URL=' "$ENV_FILE" | cut -d= -f3- | sed 's|sqlite:///||')
DATA_DIR=$(grep '^DATA_DIR=' "$ENV_FILE" | cut -d= -f2-)
mkdir -p "$DATA_DIR/backups"
if [ -f "$DB-wal" ]; then
  sqlite3 "$DB" ".backup '$DATA_DIR/backups/recon-$(date +%Y%m%d-%H%M%S).db'"
else
  cp "$DB" "$DATA_DIR/backups/recon-$(date +%Y%m%d-%H%M%S).db"
fi
ls -lt "$DATA_DIR/backups" | head -3
