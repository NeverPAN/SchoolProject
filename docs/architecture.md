# 系统架构文档

## 1. 总体架构（模块化单体后端 + 独立 Worker + Web 前端）

```text
用户浏览器（Vue 3 SPA，可由后端静态托管）
    │  HTTP/JSON（/api/v1，JWT Bearer）
    ▼
FastAPI 业务后端（uvicorn, port 8000）
    ├─ auth        注册/登录/JWT
    ├─ projects    项目 CRUD + 归属校验
    ├─ images      流式上传/元数据/缩略图/质量检查
    ├─ jobs        任务提交/状态/事件/日志/取消/恢复
    ├─ models      模型登记/版本/下载/查看数据
    ├─ SQLAlchemy 2.x ──► SQLite(WAL)（原型）/ PostgreSQL（生产）
    └─ Redis ──► RQ 队列 recon
                    │
                    ▼
        RQ Worker（单实例，串行执行 GPU 任务）
                    │
                    ▼
        重建管线 ReconstructionPipeline
                    ├─ ColmapAdapter（subprocess 数组调用，无 shell）
                    │     特征提取 → 匹配 → SfM → 去畸变 → PatchMatch → 融合 → Poisson
                    ├─ MockPipeline（无 COLMAP 环境模拟，同状态机）
                    ├─ 检查点 manifest（参数哈希+输入哈希+文件校验和）
                    └─ 导出层（点云着色/GLB 转换/预览图）
                    │
                    ▼
        存储布局
          DATA_DIR(NFS): users/{uid}/projects/{pid}/input|outputs
          SCRATCH_DIR(本地): users/{uid}/projects/{pid}/jobs/{jid}/{work,logs,checkpoints}
```

## 2. 关键设计决策

| 决策 | 依据 |
| --- | --- |
| 模块化单体 + 单 Worker | 指南 §3.1；单 A800 串行 GPU 任务，避免争抢 |
| SQLite(WAL) 起步 | 指南 §4.4 允许；容器 /dev/shm 限制使 PG 不可用（见 environment-report.md）；SQLAlchemy 隔离 |
| COLMAP CLI 而非源码 | 指南 §4.5；阶段化命令天然映射系统阶段 |
| 任务状态权威在 DB | 指南 §4.3；Redis 仅调度；Worker 启动时 requeue 卡死任务 |
| 进度为阶段权重估算 | 指南 §B1；9 阶段权重 2/13/15/15/5/25/10/10/5，界面标注"估算" |
| 原子写 manifest | 指南 §14.2；临时文件 + os.replace |
| 上传流式写盘 | 指南 §6.4；1GB 级数据不进内存 |
| 前端经 API 取 Blob | 模型/缩略图请求带 JWT 头，避免 query token 泄漏 |

## 3. 任务状态机

```text
CREATED → VALIDATING → QUEUED → RUNNING ─┬─► SUCCEEDED
                              │  ├─────► FAILED ─(resume)─► RESUMING ─► QUEUED
                              │  └─────► CANCEL_REQUESTED ─► CANCELLED ─(resume)─►
                              └─（QUEUED 未开始可直接 CANCELLED）
```

阶段（顺序执行，检查点独立目录）：IMAGE_VALIDATION → FEATURE_EXTRACTION → FEATURE_MATCHING →
SPARSE_RECONSTRUCTION → IMAGE_UNDISTORTION → DENSE_RECONSTRUCTION → STEREO_FUSION → MESHING → EXPORTING

## 4. 断点恢复机制

- 每个成功阶段在其检查点目录写 `manifest.json`：{stage, params_sha, inputs_sha, files:[{path,sha256,size}]}；
- `inputs_sha = sha256(input_manifest + config)`：输入图片清单或配置变化 → 旧检查点全部失效（拒绝错误复用）；
- resume 时逐阶段校验 manifest（参数一致 + 输入一致 + 文件存在且校验和一致）→ 有效则跳过；
- 任务级输入清单在 resume API 处先做图片清单比对，变化直接拒绝（409 INPUT_CHANGED）。

## 5. 安全设计

- 密码 bcrypt 哈希；JWT HS256（可配置有效期）；
- 所有资源接口做归属校验（非本人一律 404，不泄露存在性）；
- 上传：扩展名白名单 + PIL 真实解码校验 + 大小/数量/总量限制 + 项目内 checksum 去重 + UUID 存储名；
- 子进程：参数数组 + 固定可执行路径 + 进程组超时/取消；
- 日志过滤器防密码/Token 落盘。

## 6. 前端架构

- Vue 3 + TS + Vite + Element Plus + Pinia + Vue Router（登录守卫）；
- `three/ModelViewer.vue`：GLTFLoader/PLYLoader（ArrayBuffer+parse，带鉴权）、OrbitControls、
  自动取景、资源释放、5 种风格预设（写实/莫兰迪/单色/线框/点云）与背景预设；
- 开发模式 Vite proxy `/api → :8000`；生产由后端托管 `frontend/dist`（SPA fallback）。
