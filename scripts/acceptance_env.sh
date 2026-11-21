#!/usr/bin/env bash
# 验收环境统一启动/停止/状态脚本（一个环境 = 独立 Redis + API + N 个 Worker）。
#
# 背景：此前验收出现过三类事故——setsid 包装 PID 与真实 Worker PID 不一致导致
# pid 文件失效；残留 Worker 抢走另一环境的任务（South 单卡臂被双卡 Worker 污染）；
# scratch 落在 NFS 上使稠密迭代耗时膨胀 3.4×。本脚本以三条原则统一管理：
#   1. 启动后通过 /proc 扫描按「cmdline 指纹 + environ 指纹」捕获真实 PID 写 pid 文件；
#   2. 停止只按真实 PID 操作，且进程必须同时匹配本环境的 cmdline 与 environ
#      指纹（DATABASE_URL），pid 文件中的陈旧 PID 还需先通过指纹确认（防 PID
#      复用误杀）——历史站点（18082/18469/18479 等）的 Worker 与 Redis 永远
#      不会被误杀；
#   3. SCRATCH_DIR 默认指向本地盘 /tmp（NFS scratch 是性能事故根因）。
#
# 用法：
#   scripts/acceptance_env.sh --env <环境目录> --port <API端口> --redis <Redis端口> start|stop|status
# 可选参数：
#   --scratch <目录>      默认 /tmp/recon-<环境目录名>-scratch（本地盘）
#   --workers <N>         Worker 数量，默认 1（双卡由 --gpu-index "0,1" 控制）
#   --gpu-index "0,1"     COLMAP_GPU_INDEX，默认 "0,1"
#   --dense-size 3000     COLMAP_DENSE_MAX_IMAGE_SIZE，默认 3000
#   --colmap-bin <路径>   默认 conda colmap-gpu 环境
#   --texturer-bin <路径> 默认 /tmp 解包的 CPU 贴图构建（易失，机器重启后需重新
#                         解包或改指持久路径；不设置则照片贴图功能不可用）
#   --host <host>         API 监听地址，默认 127.0.0.1（不对外开端口）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$REPO/backend"
COLMAP_DEFAULT="/data/home/2024090904028/.conda/envs/colmap-gpu/bin/colmap"
TEXTURER_DEFAULT="/tmp/recon-colmap.oUTYXW/env/bin/colmap"

ENV_DIR="" PORT="" REDIS_PORT="" SCRATCH="" WORKERS=1 GPU_INDEX="0,1" DENSE_SIZE=3000
COLMAP_BIN="$COLMAP_DEFAULT" TEXTURER="$TEXTURER_DEFAULT" HOST="127.0.0.1" ACTION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_DIR="$(realpath "$2")"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --redis) REDIS_PORT="$2"; shift 2;;
    --scratch) SCRATCH="$(realpath "$2")"; shift 2;;
    --workers) WORKERS="$2"; shift 2;;
    --gpu-index) GPU_INDEX="$2"; shift 2;;
    --dense-size) DENSE_SIZE="$2"; shift 2;;
    --colmap-bin) COLMAP_BIN="$2"; shift 2;;
    --texturer-bin) TEXTURER="$2"; shift 2;;
    --host) HOST="$2"; shift 2;;
    start|stop|status) ACTION="$1"; shift;;
    *) echo "未知参数: $1" >&2; exit 2;;
  esac
done

[[ -n "$ENV_DIR" && -n "$PORT" && -n "$REDIS_PORT" && -n "$ACTION" ]] || {
  sed -n '2,30p' "${BASH_SOURCE[0]}" >&2; exit 2; }
[[ -n "$SCRATCH" ]] || SCRATCH="/tmp/recon-$(basename "$ENV_DIR")-scratch"
SCRATCH="$(realpath -m "$SCRATCH")"

REDIS_URL="redis://127.0.0.1:${REDIS_PORT}/0"
DB_URL="sqlite:///${ENV_DIR}/app.db"

# 在 /proc 中查找真实 PID：cmdline 必含 $1，environ 必含 $2（$2 为空则不查 environ）
find_pids() { # <cmdline 片段> <environ 片段>
  local p c e
  for p in /proc/[0-9]*; do
    [[ -r "$p/cmdline" ]] || continue
    c="$(tr '\0' ' ' 2>/dev/null <"$p/cmdline")" || continue
    [[ "$c" == *"$1"* ]] || continue
    if [[ -n "$2" ]]; then
      e="$(tr '\0' '\n' 2>/dev/null <"$p/environ")" || continue
      [[ "$e" == *"$2"* ]] || continue
    fi
    basename "$p"
  done
}

# Redis：按 proctitle（redis-server …:<port>）+ 端口数字边界匹配
redis_pids() {
  local p c
  for p in /proc/[0-9]*; do
    [[ -r "$p/cmdline" ]] || continue
    c="$(tr '\0' ' ' 2>/dev/null <"$p/cmdline")" || continue
    [[ "$c" == *redis-server* ]] || continue
    echo "$c" | grep -qE ":${REDIS_PORT}([^0-9]|$)" || continue
    basename "$p"
  done
}

# API：cmdline 含 uvicorn 入口与端口（数字边界，兼容 --host/--port 任意顺序）+ environ 指纹
api_pids() {
  local p c e
  for p in /proc/[0-9]*; do
    [[ -r "$p/cmdline" ]] || continue
    c="$(tr '\0' ' ' 2>/dev/null <"$p/cmdline")" || continue
    [[ "$c" == *"uvicorn app.main:app"* ]] || continue
    echo "$c" | grep -qE "(^|[^0-9])${PORT}([^0-9]|$)" || continue
    e="$(tr '\0' '\n' 2>/dev/null <"$p/environ")" || continue
    [[ "$e" == *"DATABASE_URL=$DB_URL"* ]] || continue
    basename "$p"
  done
}

# Worker：cmdline + environ 双指纹
worker_pids_of() {
  find_pids "rq worker -u $REDIS_URL recon" "DATABASE_URL=$DB_URL"
}

# 等待收集函数产出的 PID 数达到 need，达到即输出其 PID 列表
wait_for() { # <need> <收集函数名> <超时秒>
  local need="$1" fn="$2" timeout="$3" n=""
  local deadline=$(( $(date +%s) + timeout ))
  while (( $(date +%s) < deadline )); do
    n=$("$fn" | wc -l)
    (( n >= need )) && { "$fn"; return 0; }
    sleep 1
  done
  echo "等待进程出现超时: $fn（期望 ≥$need，实得 ${n:-0}）" >&2
  return 1
}

health_ok() {
  curl -sf "http://127.0.0.1:${PORT}/api/v1/health" 2>/dev/null | grep -q '"ok"'
}

do_start() {
  mkdir -p "$ENV_DIR" "$SCRATCH" "$ENV_DIR/data" "$ENV_DIR/redis"
  [[ -f "$ENV_DIR/empty.env" ]] || : >"$ENV_DIR/empty.env"

  # 1) Redis（已在运行则复用）
  if [[ -z "$(redis_pids)" ]]; then
    setsid redis-server --port "$REDIS_PORT" --bind 127.0.0.1 \
      --dir "$ENV_DIR/redis" --save '' --appendonly no \
      --logfile "$ENV_DIR/redis.log" </dev/null >/dev/null 2>&1 &
    sleep 1
  fi
  local up=""
  for _ in $(seq 1 10); do
    redis-cli -p "$REDIS_PORT" ping 2>/dev/null | grep -q PONG && { up=1; break; }
    sleep 1
  done
  [[ -n "$up" ]] || { echo "Redis 启动失败（端口 $REDIS_PORT）" >&2; exit 1; }
  local redis_real; redis_real=$(redis_pids | head -1)
  echo "$redis_real" >"$ENV_DIR/redis.pid"

  # 2) API（已在运行则复用；setsid + env 注入环境变量，不依赖包装 PID）
  if [[ -z "$(api_pids)" ]]; then
    ( cd "$BACKEND" && setsid env \
        RECON_ENV_FILE="$ENV_DIR/empty.env" \
        DATABASE_URL="$DB_URL" \
        DATA_DIR="$ENV_DIR/data" \
        SCRATCH_DIR="$SCRATCH" \
        RECON_MODE=colmap RECON_USE_GPU=1 \
        COLMAP_BIN="$COLMAP_BIN" \
        COLMAP_GPU_INDEX="$GPU_INDEX" \
        COLMAP_DENSE_MAX_IMAGE_SIZE="$DENSE_SIZE" \
        COLMAP_TEXTURER_BIN="$TEXTURER" \
        REDIS_URL="$REDIS_URL" \
        LOG_LEVEL=INFO \
        .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" \
        >>"$ENV_DIR/api.log" 2>&1 </dev/null & )
  fi
  local api_real
  api_real=$(wait_for 1 api_pids 30 | head -1)
  echo "$api_real" >"$ENV_DIR/api.pid"

  # 3) Worker（可多个；已有的复用，只补足到 --workers 数量）
  local have
  have=$(worker_pids_of | grep -c . || true)
  if (( have < WORKERS )); then
    for ((i = have + 1; i <= WORKERS; i++)); do
      ( cd "$BACKEND" && setsid env \
          RECON_ENV_FILE="$ENV_DIR/empty.env" \
          DATABASE_URL="$DB_URL" \
          DATA_DIR="$ENV_DIR/data" \
          SCRATCH_DIR="$SCRATCH" \
          RECON_MODE=colmap RECON_USE_GPU=1 \
          COLMAP_BIN="$COLMAP_BIN" \
          COLMAP_GPU_INDEX="$GPU_INDEX" \
          COLMAP_DENSE_MAX_IMAGE_SIZE="$DENSE_SIZE" \
          COLMAP_TEXTURER_BIN="$TEXTURER" \
          REDIS_URL="$REDIS_URL" \
          LOG_LEVEL=INFO \
          .venv/bin/rq worker -u "$REDIS_URL" recon \
          >>"$ENV_DIR/worker.log" 2>&1 </dev/null & )
    done
  fi
  local worker_real
  worker_real=$(wait_for "$WORKERS" worker_pids_of 30)
  echo "$worker_real" | head -1 >"$ENV_DIR/worker.pid"
  echo "$worker_real" >"$ENV_DIR/workers.pid"

  # 4) 健康检查（最多 30 秒）
  local ok=""
  for _ in $(seq 1 15); do
    if health_ok; then ok=1; break; fi; sleep 2
  done
  [[ -n "$ok" ]] || { echo "API 健康检查未通过" >&2; exit 1; }

  # 5) 记录环境指纹，供追溯与 E2E 校验
  cat >"$ENV_DIR/env.json" <<EOF
{"api_port": $PORT, "redis_port": $REDIS_PORT, "scratch_dir": "$SCRATCH",
 "gpu_index": "$GPU_INDEX", "dense_max_image_size": $DENSE_SIZE,
 "colmap_bin": "$COLMAP_BIN", "texturer_bin": "$TEXTURER",
 "workers": $WORKERS, "api_pid": $api_real, "redis_pid": $redis_real}
EOF
  echo "环境就绪：API=$PORT（pid $api_real）Worker=$(echo $worker_real | tr '\n' ' ')Redis=$REDIS_PORT（pid $redis_real）scratch=$SCRATCH"
}

do_stop() {
  local pids="" p rest
  # 候选 = 实时指纹扫描结果（已是本环境进程）+ pid 文件记录（需过指纹确认）
  pids="$pids $(api_pids) $(worker_pids_of)"
  while read -r p; do
    [[ "$p" =~ ^[0-9]+$ ]] || continue
    api_pids | grep -qx "$p" && pids="$pids $p"
  done 2>/dev/null <"$ENV_DIR/api.pid" || true
  while read -r p; do
    [[ "$p" =~ ^[0-9]+$ ]] || continue
    worker_pids_of | grep -qx "$p" && pids="$pids $p"
  done 2>/dev/null <"$ENV_DIR/workers.pid" || true
  for p in $pids; do kill "$p" 2>/dev/null || true; done
  # 最多等 8 秒，仍存活则 SIGKILL
  for _ in $(seq 1 8); do
    rest=""
    for p in $pids; do [[ -d "/proc/$p" ]] && rest="$rest $p"; done
    [[ -z "${rest// /}" ]] && break
    sleep 1
  done
  for p in $pids; do [[ -d "/proc/$p" ]] && kill -9 "$p" 2>/dev/null || true; done
  # Redis 最后停
  for p in $(redis_pids); do kill "$p" 2>/dev/null || true; done
  sleep 2
  for p in $(redis_pids); do [[ -d "/proc/$p" ]] && kill -9 "$p" 2>/dev/null || true; done
  # 确认全部消失
  local left
  left="$(api_pids)$(worker_pids_of)$(redis_pids)"
  [[ -z "$(echo "$left" | tr -d '[:space:]')" ]] && echo "环境已停止" || {
    echo "警告：仍有残留进程 $left" >&2; exit 1; }
}

do_status() {
  local api="停止" workers="停止" redis="停止" h="不可达"
  local live_api live_workers
  live_api=$(api_pids | tr '\n' ' ')
  [[ -n "${live_api// /}" ]] && api="运行中 pid=$live_api"
  live_workers=$(worker_pids_of | tr '\n' ' ')
  [[ -n "${live_workers// /}" ]] && workers="运行中 pid=$live_workers"
  local rp; rp=$(redis_pids | head -1)
  [[ -n "$rp" ]] && redis="运行中 pid=$rp"
  health_ok && h="ok"
  echo "API($PORT): $api | Worker: $workers | Redis($REDIS_PORT): $redis | health: $h"
  echo "scratch: $SCRATCH | gpu-index: $GPU_INDEX | dense-size: $DENSE_SIZE"
  [[ "$h" == "ok" && "$api" == *"运行中"* && "$workers" != "停止" ]] || exit 1
}

case "$ACTION" in
  start) do_start;;
  stop) do_stop;;
  status) do_status;;
esac
