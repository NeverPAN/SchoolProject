# 部署与演示交接

以下命令以仓库根目录和 Linux x86_64 为基准。仅在获授权的目标环境执行；不要覆盖现有 `.env`、数据库、模型或上传文件。Windows 演示可使用 WSL2，也可直接使用离线模型交接包。没有计划停机时不要在正在提供服务的工作树运行 `npm ci`、迁移或替换 `dist`。

## 1. 已验证的版本

| 组件 | 复现版本 / 构建 |
| --- | --- |
| 后端 | CPython 3.10.6 / SQLite 3.39.3；Python 包见 [`requirements-linux-py310.lock`](../backend/requirements-linux-py310.lock) |
| 前端 | Node.js 22.23.5 / npm 10.9.8；使用 `frontend/package-lock.json` 和 `npm ci --engine-strict`。锁内 VueUse 要求 Node ≥ 22 |
| 队列 | Redis Server 6.0.16 |
| 主重建引擎 | conda-forge `colmap=3.13.0=cuda_129h1e19e12_4`（CUDA）；`colmap -h` 须显示 `with CUDA`。无 CUDA 的 COLMAP 3.7 只能用于稀疏重建，不能代替 CUDA 稠密流程 |
| 实验贴图引擎 | conda-forge `colmap=4.2.1=cpu_gpl_h68f24ef_408`（独立 CPU 环境） |

这里锁定的是复现用版本，不表示这些旧运行时应暴露于公网；目标机需另行核验 GPU 驱动、Redis 访问控制及系统安全更新。两个 COLMAP 包固定构建号；同一构建的跨主机复现建议在已验证机器上分别运行 `conda list --explicit -n colmap-gpu` 和 `conda list --explicit -n colmap-texture-cpu`，保存输出并用 `conda create --file <对应清单>` 创建独立环境。清单本身不包含离线安装包。

## 2. 构建与启动

在新部署目录中，使用上表版本对应的 `python3.10`、`node`、`npm`：

```bash
python3.10 -m venv backend/.venv
backend/.venv/bin/python -m pip install --only-binary=:all: -r backend/requirements-linux-py310.lock
backend/.venv/bin/python -m pip check
(cd frontend && npm ci --engine-strict && npm run build)
```

明确需要真实重建时，使用可用的既有引擎或在获授权的用户 Conda 环境中安装以下构建。Mock 演示不需要安装任何 COLMAP：

```bash
conda create -n colmap-gpu --override-channels -c conda-forge 'colmap=3.13.0=cuda_129h1e19e12_4'
# 可选：完全独立的 CPU 贴图环境
conda create -n colmap-texture-cpu --override-channels -c conda-forge 'colmap=4.2.1=cpu_gpl_h68f24ef_408'
```

首次配置可以从 `.env.example` 创建 `.env`；已有部署请保留原文件。API、Worker 和迁移必须使用同一份配置（可在三个进程中导出相同的绝对路径 `RECON_ENV_FILE`）。设置随机 `SECRET_KEY`、批准使用的 `REDIS_URL`、持久化 `DATA_DIR` 和独立的 `SCRATCH_DIR`。SQLite 用持久化本地盘，避免通过网络文件系统共享可写 SQLite。正式 PostgreSQL 部署需另行配置服务器、驱动和备份，此锁验证范围为 SQLite。

真实重建设置 `RECON_MODE=colmap`，将 `COLMAP_BIN` 指向主 CUDA 构建的绝对路径。检查 `nvidia-smi` 和 `"$COLMAP_BIN" -h` 后再启用 GPU。仅在需要尝试新任务的照片贴图时设置 `COLMAP_TEXTURER_BIN=/absolute/path/to/colmap-texture-cpu/bin/colmap`，并预检该二进制的 `mesh_texturer -h`。这不替代 `COLMAP_BIN`，也不改变 `RECON_USE_GPU`。缺少 CPU 配置、任务未产出可用网格/去畸变工作区或贴图失败时，不应把普通 GLB 当成照片贴图。移除可选变量即可恢复默认；现有模型不需重新生成。仅作流程演示用 `RECON_MODE=mock`，并明确标示模拟产物。

初始化空库（存量库迁移前应先备份，并按维护窗口安排）：

```bash
(cd backend && .venv/bin/python -m alembic upgrade head)
```

确认获授权的 Redis 实例可用后，在不同终端或受批准的进程管理器启动（不要重复启动现有 Worker）：

```bash
bash scripts/run_local_backend.sh --host 127.0.0.1
bash scripts/run_worker.sh
```

FastAPI 在启动时挂载 `frontend/dist`，入口为 `http://127.0.0.1:8000/`、API 文档为 `/api/docs`。仅开发时可另外用 `bash scripts/run_frontend.sh` 启动 Vite。在远程环境按[远程访问指南](access-guide.md)转发端口。检查以下端点，并查看 Worker 日志确认进程监听预期队列（Redis 可连接不等于 Worker 在消费）：

```bash
curl -fsS http://127.0.0.1:8000/api/v1/health
curl -fsS http://127.0.0.1:8000/api/v1/health/db
curl -fsS http://127.0.0.1:8000/api/v1/health/worker
```

## 3. SQLite 备份、隔离恢复与证据

```bash
# 日常备份：使用应用数据库设置；默认输出到 DATA_DIR/backups
bash scripts/backup_db.sh

# 或指定现有源文件与独立存储目录；参数可包含空格
bash scripts/backup_db.sh --source /absolute/path/live.db --output-dir /absolute/path/backups

# --backup 使用上一步 JSON 中的 bundle；--destination 必须不存在且父目录已存在
backend/.venv/bin/python -I scripts/sqlite_backup.py restore \
  --backup /absolute/path/backups/recon-EXAMPLE \
  --destination /absolute/path/isolated-restore

# 合成演练：新临时目录、新数据库；不会读写应用数据库，不使用 Redis 或服务
backend/.venv/bin/python -I scripts/drill_sqlite_restore.py
```

备份使用 SQLite 在线备份 API，包含备份读事务可见的已提交 WAL 内容，不复制活动主库文件。成功产物是 `recon.db` 和 `manifest.json`，两者必须一起保存；只使用已成功结束且清单齐全的备份。恢复核对大小、SHA-256 和 `PRAGMA integrity_check`，生成 `restore-evidence.json`；已有目标目录一律拒绝。校验和可发现损坏，不是签名，清单与备份必须来自可信存储。演练另外核对迁移版本、外键，以及已提交 WAL 数据可恢复、未提交数据不恢复、拒绝篡改/已有目标/缺失源。终端输出及临时目录中的 `drill-evidence.json` 记录演练结果。

这是数据库元数据备份，不包含图片、模型、Redis 队列或中间文件。另行备份 `DATA_DIR` 中的用户资产，并为一致性要求较高的应用恢复安排停止写入的窗口。不要把演练结果复制到在线数据库上；真正切换应另行批准，保留原库和 sidecar，并先在独立 API/Worker 配置中核对资产路径与权限。备份目录需限制访问、设置保留周期并将成功备份转存到独立故障域。

## 4. 用户操作与断网演示

1. 打开首页，注册或使用专用演示账号登录；不要使用真实账户或展示凭据。创建演示项目，批量上传同一对象的多视角、经授权的照片；先检查缩略图与质量提示。
2. 选择重建预设后提交任务，打开任务详情，展示排队、阶段进度和日志。长时间重建应提前完成；现场提交可演示队列，不要依赖它在答辩时间内完成。
3. 打开已完成项目的模型列表，进入预览，旋转、缩放，切换可用视图/材质，下载模型。照片贴图与普通预览应分别展示，并说明未观测区域、孔洞及任务中的贴图提示。真实示例的引擎、产物与限制见[照片贴图演示](photo-texture-demo.md)。

断网交接建议准备两条路径：

- **模型展示兜底**：打包已验证的自包含 `.glb`、必要的 `.ply`、代表截图、任务日志摘要、来源及许可、版本和哈希。将文件在演示机上用离线 GLB 查看器预打开，确认断网仍能转动与缩放。不要依赖集群 URL、SSH、CDN 或 `file://` 打开 Vite 的构建首页。
- **本地完整流程**：在同平台联网构建机上预生成 `frontend/dist`，并准备 Python 安装介质、Redis、浏览器与后端 wheelhouse。演示机放置完整构建产物到 `frontend/dist`，用全新的 SQLite/存储/Redis 及专用账号启动 Mock 配置。把真实产物用于独立展示，不把 Mock 的结果标成真实重建。提前断网走一遍登录、建项目、上传、任务、预览和下载。

制作和使用 wheelhouse（同 Linux x86_64/CPython 3.10）：

```bash
# 联网打包；交接 SHA256SUMS 并通过独立可信渠道核对
backend/.venv/bin/python -m pip download --only-binary=:all: \
  --dest wheelhouse -r backend/requirements-linux-py310.lock
(cd wheelhouse && sha256sum *.whl > SHA256SUMS)

# 离线演示机；校验后安装到新虚拟环境
(cd wheelhouse && sha256sum -c SHA256SUMS)
python3.10 -m venv backend/.venv
backend/.venv/bin/python -m pip install --no-index --find-links wheelhouse \
  --only-binary=:all: -r backend/requirements-linux-py310.lock
```

完整流程的隔离配置示例（仓库根目录，端口均应空闲；以下为演示机操作，不在现有部署上执行）：

```bash
umask 077
DEMO_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/recon-offline.XXXXXX")
export RECON_ENV_FILE="$DEMO_ROOT/demo.env" BACKEND_PORT=18000
printf 'DATABASE_URL=sqlite:///%s/recon.db\nDATA_DIR=%s/data\nSCRATCH_DIR=%s/scratch\n' \
  "$DEMO_ROOT" "$DEMO_ROOT" "$DEMO_ROOT" > "$RECON_ENV_FILE"
printf 'RECON_MODE=mock\nRECON_USE_GPU=0\nCOLMAP_TEXTURER_BIN=\nREDIS_URL=redis://127.0.0.1:16379/0\n' >> "$RECON_ENV_FILE"
printf 'SECRET_KEY=%s\n' "$(backend/.venv/bin/python -I -c 'import secrets; print(secrets.token_hex(32))')" >> "$RECON_ENV_FILE"
(cd backend && .venv/bin/python -m alembic upgrade head)
```

若当前 shell 已导出应用变量，展开配置前先打开干净终端，避免环境变量覆盖演示文件。用不同终端启动 `redis-server --bind 127.0.0.1 --port 16379 --save '' --appendonly no`、`bash scripts/run_worker.sh` 和 `bash scripts/run_local_backend.sh --host 127.0.0.1`。后两个终端均需导出上面的 `RECON_ENV_FILE` 和 `BACKEND_PORT`，再访问 `http://127.0.0.1:18000/`。演示结束用 Ctrl+C 停止本次启动的三个前台进程；SQLite 和模型仍保留在 `DEMO_ROOT`，Redis 队列为临时数据。

不交接真实数据库、生产 `.env`、访问令牌、真实账户或无权分享的原始照片。演示包、轮子和模型是交接产物，不提交到 Git。
