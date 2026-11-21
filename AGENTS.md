# AGENTS.md — AI Agent 协作规范

> 任何 AI Agent（Codex / Claude Code / 其他）在本仓库工作前必须先阅读本文件与
> `DEVELOPMENT_GUIDE.md`（根目录副本，原始文件见 `../Project_reco/图片三维重建系统_综设开发指南.md`）。

## 必须遵守

1. 开始任务前必须阅读 `DEVELOPMENT_GUIDE.md` 与 `README.md`，以及任务涉及的模块文档。
2. 不得擅自改变系统架构（模块化单体后端 + 独立 Worker + Vue 前端）与已发布的 API 契约。
3. 不得编造测试结果。所有性能/质量数据必须来自真实运行的日志与记录。
4. 不得把大型数据集、模型文件、`.env`、密钥提交到 Git（见 `.gitignore`）。
5. 修改数据库结构必须创建 Alembic migration，不得手改已发布的迁移。
6. 新增功能必须附带测试；修改重建流程必须保留阶段状态与任务日志。
7. 不得在没有明确授权时连接学校集群、修改服务器系统级配置或执行破坏性命令。
8. 所有命令与配置必须考虑 Windows 本地开发与 Ubuntu 容器运行的差异；路径配置全部走 `.env`，不硬编码。
9. 长时间任务不得在 HTTP 请求内同步执行，必须走 Redis + RQ Worker。
10. 每次任务完成后报告：修改文件、实现内容、测试命令、测试结果、未解决问题。
11. 每个开发阶段/轮次结束必须写总结文档并更新索引（见下方「文档规范」），不写不算完成。

## 工作流程

1. 理解任务（阅读文档与现有代码，明确输入、输出、依赖、验收条件）。
2. 先分析后修改（给出简短实施计划，指出涉及的模块与风险）。
3. 小步实现（一次一个明确功能）。
4. 执行测试（后端 `pytest`，前端 `npm run build`，必要时端到端脚本）。
5. 检查差异（是否误改接口、配置、依赖、数据模型）。
6. 总结交付（完成内容、测试结果、未完成项、下一步建议）。

## 关键技术约定

| 项 | 约定 |
| --- | --- |
| API 前缀 | `/api/v1`，JSON 响应，错误码稳定 |
| 认证 | JWT Bearer Token（`Authorization: Bearer <token>`） |
| 数据库 | PostgreSQL（生产）；SQLAlchemy 2.x + Alembic；测试可用 SQLite |
| 任务队列 | Redis + RQ，单 Worker 串行执行 GPU 任务 |
| 重建引擎 | COLMAP CLI（`subprocess` 参数数组调用，禁止 `shell=True`） |
| 文件存储 | `DATA_DIR` 下按 `users/{user_id}/projects/{project_id}/...` 组织 |
| 模型展示 | Three.js + GLTFLoader/PLYLoader + OrbitControls |
| 上传 | 流式写入磁盘，限制类型/大小，记录 SHA-256 |
| 取消/恢复 | 阶段边界安全取消；恢复前校验 manifest 与输出完整性 |

## 文档规范

> 背景：第一阶段/第二阶段/质量修复轮的完整总结见 [docs/dev-summary/00-overview.md](docs/dev-summary/00-overview.md)。

1. 每个开发阶段或轮次结束，在 `docs/` 写总结文档（架构改动、测试命令与真实结果、问题与解决方案、未解决项与边界），并更新 [docs/dev-summary/00-overview.md](docs/dev-summary/00-overview.md) 索引。
2. 图文并茂：架构/流程用 mermaid；对比图与截图放 `docs/dev-summary/assets/` 或既有证据目录；原始运行数据留在 `data/`（不入 Git），文档引用其结论与必要截图。
3. 所有数字来自真实运行的日志与记录，标注采样区间与统计口径；口径变化时同时给出旧口径数据。
4. 证据文件（日志/JSON/截图）提交到 `docs/*-evidence/` 或 `tests/`，文件名可检索。

## 目录速览

```text
3d-reconstruction-system/
  backend/    FastAPI 应用、COLMAP 适配层、RQ Worker、pytest 测试
  frontend/   Vue 3 + TS + Vite + Element Plus + Three.js
  scripts/    启动脚本、数据集生成、端到端测试脚本
  docs/       需求、架构、API 契约、数据库设计、测试计划、环境报告
  sample-data/ 小型合成测试数据集（不入 Git，见 sample-data/README.md）
  data/       运行时数据（不入 Git）
```
