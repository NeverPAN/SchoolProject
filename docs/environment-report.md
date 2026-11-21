# 环境报告（阶段 0 交付物）

> 检测日期：2026-11-18　检测方式：容器内实测（非推断）
> 环境：学校 A800 GPU 服务器的 Ubuntu Docker 容器

## 1. 硬件与系统

| 项目 | 实测值 |
| --- | --- |
| GPU | 2 × NVIDIA A800 80GB PCIe（驱动 570.195.03，CUDA 12.8） |
| CPU | Intel Xeon Gold 6348 @ 2.60GHz，容器内可见 **56 核** |
| 内存 | 1.0 TiB 总量，可用约 970 GiB |
| 系统 | Ubuntu 22.04.3 LTS（Docker 容器内，存在 /.dockerenv） |
| 网络 | 外网可用（pypi/npm/conda-forge/github 均可访问） |

## 2. 存储布局与持久化

| 路径 | 文件系统 | 用途 | 持久性 |
| --- | --- | --- | --- |
| `/data/home/2024090904028` | NFS（hdd.data，3.3PB 可用） | 用户数据（图片/模型/输出） | **容器重建后保留** |
| `/`（overlay） | 本地盘 3.5T（1.4T 可用） | 数据库、临时/中间结果 | 容器删除即丢失 |
| `/dev/shm` | tmpfs **仅 8KB** | — | 见已知限制 |

**存储策略**：
- `DATA_DIR`（用户数据）→ NFS home，随容器持久化；
- `DATABASE_URL`（SQLite WAL）与 `SCRATCH_DIR`（COLMAP 中间结果）→ 本地 overlay（性能与锁可靠性优先）；
- 提供脚本 `scripts/backup_db.sh` 将数据库定期备份到 NFS（建议 cron 执行）。

## 3. 软件组件（实测版本）

| 组件 | 版本 | 安装方式 | 备注 |
| --- | --- | --- | --- |
| Python | 3.10.12 | 容器自带（miniconda） | |
| Node.js | 18.9.0 | 容器自带 | Vite 5 / Vue 3 兼容 |
| Redis | 6.0.16 | `apt install redis-server` | 任务队列 |
| PostgreSQL | 14.24 | `apt install postgresql` | **未启用**（见限制 1） |
| COLMAP（CPU） | 3.7.0 | `apt install colmap` | 无 CUDA，`patch_match_stereo` 不可用 |
| COLMAP（GPU） | **3.13.0** | conda-forge `colmap-gpu` 环境 | **with CUDA**，稠密重建可用 |
| SQLite | 3.x（stdlib） | — | WAL 模式，并发实测 0.16s/400 写 |

CUDA 版 COLMAP 路径：`/data/home/2024090904028/.conda/envs/colmap-gpu/bin/colmap`（`.env` 中 `COLMAP_BIN` 指向）。
GPU 实测：`patch_match_stereo` 单图深度图 + 几何一致性约 2s/迭代量级，A800 显存占用 <6GB（40 图工作区）。

## 4. 已知限制与应对

1. **PostgreSQL 无法初始化**：容器 `/dev/shm` 仅 8KB 且无权限 remount；PG14 的 mmap 共享内存默认落在 /dev/shm，initdb 阶段即失败（`could not resize shared memory segment`）。PGOPTIONS/PG16 方案均不可行。
   **应对**：第一阶段按开发指南 §4.4 允许的方案使用 SQLite（WAL + 本地盘 + 定期备份）；系统通过 SQLAlchemy 抽象，生产切换 PostgreSQL 仅需修改 `DATABASE_URL` 并重跑 Alembic（需管理员扩容 /dev/shm 至 ≥1GB）。
2. **SQLite 与 NFS 不兼容**：实测 NFS 上 WAL 并发写近乎卡死（锁语义问题）。
   **应对**：数据库放本地 overlay，不放 NFS。
3. **COLMAP 3.13 与 3.7 的差异**（均已实测并适配）：
   - GPU 选项名：`SiftExtraction.use_gpu` → `FeatureExtraction.use_gpu`（SIFT 算法参数前缀不变）；
   - `--version` 不再支持（改用 `help` 探测）；
   - 稠密输出布局：`dense/{images,sparse,stereo}`（3.7 为扁平结构）；
   - **符号链接图片会被解析为规范路径**并用作图片名，导致 undistorter 沿绝对路径建目录崩溃 → 管线采用复制而非符号链接。
4. **GPU 并发**：多任务共享 A800 时利用率均达 100%，显存充足（80GB），但性能数据应记录并发状态。
5. **网络入口**：集群无公网 Web 入口，仅 VPN+SSH；按指南 §B5 以"本地全功能 + 授权成员集群操作"模式运行。

## 5. 环境预检命令输出（摘要）

```
nvidia-smi  → 2×A800 80GB, Driver 570.195.03, CUDA 12.8
nproc       → 56
free -h     → 1.0Ti total
df -h       → overlay 3.5T（59% 用量）；NFS home 3.3P 可用
colmap -h   → apt 版 3.7 without CUDA；conda 版 3.13.0 with CUDA
redis-cli ping → PONG
```
