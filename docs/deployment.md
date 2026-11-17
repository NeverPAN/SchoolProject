# 部署说明

## 1. 环境要求

- Ubuntu 20.04/22.04（容器或物理机均可）
- （可选）NVIDIA GPU + 驱动 ≥ 550（COLMAP CUDA 稠密重建需要）
- Python ≥ 3.10、Node.js ≥ 18
- Redis ≥ 6

## 2. 安装依赖

```bash
# 系统包（容器内）
sudo apt-get update && sudo apt-get install -y redis-server

# 可选：apt 版 COLMAP（3.7 CPU 版，仅稀疏流程）
sudo apt-get install -y colmap

# 推荐：conda CUDA 版 COLMAP（稠密重建可用）
conda create -y -n colmap-gpu -c conda-forge "colmap=*=cuda*"

# 后端
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 前端
cd ../frontend && npm install
```

## 3. 配置

```bash
cp .env.example .env
# 按环境修改：
# DATABASE_URL   SQLite（默认）或 PostgreSQL
# DATA_DIR       用户数据目录（建议持久化/NFS）
# SCRATCH_DIR    中间结果目录（建议本地盘）
# COLMAP_BIN     COLMAP 可执行文件路径
# RECON_USE_GPU  auto | 1 | 0
# RECON_MODE     colmap | mock
```

初始化数据库（Alembic）：

```bash
cd backend && alembic upgrade head
```

## 4. 启动服务

```bash
# Redis
redis-server --daemonize yes

# 后端 API（8000 端口）
bash scripts/run_local_backend.sh

# 重建 Worker（单实例；多开需修改 rq_worker 中 worker 名称）
bash scripts/run_worker.sh

# 前端（开发模式，5173 端口）
bash scripts/run_frontend.sh
```

## 5. 生产部署（前端静态托管）

```bash
cd frontend && npm run build
# 重启后端：FastAPI 自动托管 frontend/dist（SPA fallback）
# 访问 http://<host>:8000/ 即完整系统；API 文档在 /api/docs
```

## 6. 数据库维护

```bash
# 建议以 cron 定期备份（SQLite 位于本地盘，容器重建会丢失）
bash scripts/backup_db.sh

# Schema 变更
cd backend && alembic revision --autogenerate -m "<描述>" && alembic upgrade head
```

## 7. GPU 重建注意事项

- `nvidia-smi` 可见 GPU 不代表 COLMAP 可用 CUDA：执行 `colmap -h` 确认输出含 "CUDA"；
- 单 Worker 串行执行；多任务并行会争抢 GPU（第一版不支持并发）；
- 稠密重建显存与图片数量/分辨率相关（40 图 7MP 工作区 <6GB）。

## 8. 健康检查

```bash
curl http://127.0.0.1:8000/api/v1/health          # 服务/COLMAP/GPU
curl http://127.0.0.1:8000/api/v1/health/db       # 数据库
curl http://127.0.0.1:8000/api/v1/health/worker   # Redis/队列
```
