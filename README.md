# 图片三维重建系统（3D Reconstruction System）

集三维重建、模型管理与云端计算于一体的图片三维重建系统。用户通过 Web 界面上传多视角图片，
系统调用 COLMAP 完成特征提取、特征匹配、相机参数估计、稀疏重建、稠密重建、网格生成，
并在浏览器中交互展示与下载模型。

> **开发总结（两阶段 + 质量修复轮，图文并茂）**：[docs/dev-summary/00-overview.md](docs/dev-summary/00-overview.md)
> —— 架构总图、任务生命周期、各阶段测试与结果、问题→解决方案目录、文档规范。

## 功能总览（P0）

- 用户注册 / 登录（JWT），数据按用户隔离
- 项目创建与管理
- 图片批量上传、缩略图、分辨率/EXIF 元数据、质量检查
- 重建任务提交、阶段化状态机、进度展示、日志查看、取消、断点恢复
- COLMAP 全流程：特征提取 → 特征匹配 → 稀疏重建 → 去畸变 → 稠密重建 → 点云融合 → 网格生成 → 导出 GLB
- 模型管理：点云/网格/GLB、版本记录、下载
- Three.js 三维查看器：旋转缩放平移、点云/网格/纹理切换、风格预设（写实/莫兰迪/单色/线框/点云）
- Mock 重建模式：无 COLMAP 环境下可模拟全流程（用于本地开发与演示）

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue 3 + TypeScript + Vite + Element Plus + Pinia + Three.js |
| 后端 | Python 3.10 + FastAPI + SQLAlchemy 2.x + Alembic + Pydantic v2 |
| 数据库 | PostgreSQL（开发原型可用 SQLite） |
| 任务队列 | Redis + RQ |
| 重建引擎 | COLMAP CLI（支持 Mock 模式） |
| 图像处理 | Pillow + OpenCV |
| 测试 | pytest（后端）、Vite build（前端）、端到端脚本 |

## 快速开始（Ubuntu 容器/服务器）

```bash
# 0. 依赖（容器内）
sudo apt-get install -y redis-server postgresql colmap   # colmap 也可以用 conda-forge CUDA 版

# 1. 后端
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env   # 按需修改
alembic upgrade head
bash ../scripts/run_local_backend.sh   # 或 uvicorn app.main:app --reload --port 8000

# 2. Redis（容器内启动）
redis-server --daemonize yes

# 3. 重建 Worker（另开终端）
bash ../scripts/run_worker.sh

# 4. 前端（另开终端）
cd frontend
npm install
npm run dev    # http://localhost:5173
```

生产/集群部署时前端 `npm run build` 产物可由后端静态托管（后端已配置挂载 `frontend/dist`）。

## Mock 模式（无 COLMAP）

在 `.env` 中设置 `RECON_MODE=mock`，任务将模拟各阶段（带延时）并输出预置模型文件，
完整走通"上传 → 重建 → 查看 → 下载"链路，便于本地开发、前端联调与演示。

## 测试

```bash
cd backend && pytest -v                 # 后端单元 + 集成测试
python ../scripts/e2e_test.py           # 端到端：注册→建项目→上传→重建→查看→下载
cd frontend && npm run build            # 前端类型检查 + 构建
```

## 文档

- [图文用户手册](docs/user-manual.md)
- [三个演示版本启动说明](docs/三个演示版本启动说明.txt)
- [课程设计报告框架](docs/course-report-outline.md) · [课程设计报告正文初稿](docs/course-report.md)

- `DEVELOPMENT_GUIDE.md` — 项目总指南（需求/架构/流程/验收）
- `docs/architecture.md` — 系统架构
- `docs/api-contract.md` — API 契约
- `docs/database-design.md` — 数据库设计
- `docs/reconstruction-pipeline.md` — 重建管线与阶段映射
- `docs/testing-plan.md` — 测试计划与报告
- `docs/environment-report.md` — 运行环境报告（A800 服务器）
- `docs/deployment.md` — 部署、备份恢复与离线演示
- [第二阶段验收报告](docs/phase2-acceptance.md) — 正式数据、在线贴图、故障恢复及性能对照

## 目录结构

```text
3d-reconstruction-system/
  backend/
    app/
      api/v1/        REST 路由（auth/projects/images/jobs/models）
      core/          配置、安全、日志
      db/            SQLAlchemy 会话与初始化
      models/        ORM 模型
      schemas/       Pydantic 模型
      services/      业务服务（项目/图片/任务/模型/质量检查）
      reconstruction/ COLMAP 适配层、阶段化 Pipeline、Mock 管线、manifest
      workers/       RQ Worker 与任务函数
    migrations/      Alembic
    tests/           pytest
  frontend/
    src/
      api/           Axios 封装
      stores/        Pinia（auth/projects）
      views/         页面（登录/项目/任务/模型）
      three/         ModelViewer.vue、材质/风格预设
  scripts/           run_*.sh、generate_test_dataset.py、e2e_test.py
  docs/ deploy/ sample-data/ tests/
```
