# 测试计划与报告（第一阶段）

## 1. 测试分层

| 层级 | 工具 | 覆盖 |
| --- | --- | --- |
| 单元测试 | pytest | 密码哈希/JWT、文件名清洗、路径穿越防护、manifest 校验和 |
| API 集成测试 | pytest + TestClient | 认证、项目 CRUD、图片上传/元数据/质量检查、任务状态机、取消/恢复、模型登记/下载、用户隔离 |
| 管线集成测试 | pytest（Mock 管线） | 阶段事件完整性、进度、断点跳过、失败定位 |
| 端到端测试 | scripts/e2e_test.py | 真实服务+真实数据：上传→质量检查→重建→事件→模型→下载 |
| 前端测试 | Vite 构建 + 浏览器 GUI 实测 | 构建通过；页面流程与三维查看器交互 |

## 2. 后端 pytest 结果（2026-11-18）

```text
$ cd backend && pytest tests/ -q
50 passed in 11.35s
```

覆盖要点（对应指南验收标准）：
- 未登录访问受保护接口 → 401（test_auth.py::test_me_requires_auth）
- 用户 B 无法读取/修改/删除/上传/提交任务到用户 A 的项目 → 404（test_projects.py::TestOwnershipIsolation）
- 密码不明文存储、加盐（test_auth.py::TestPasswordHashing）
- 上传后文件数/总大小与记录一致（test_images.py::test_upload_batch_consistency）
- 非法扩展名/损坏文件/重复内容/路径穿越文件名被正确处理（test_images.py）
- 质量检查输出可查看（test_images.py::TestQualityCheck）
- 任务全链路：阶段事件完整、日志可查、不允许不可解释状态（test_jobs.py::test_full_success_flow）
- 失败定位到具体阶段与错误码（test_jobs.py::TestResume）
- 断点恢复跳过已完成阶段 ≥3 个检查点命中（test_resume_after_failure_skips_completed_stages）
- 输入变化后拒绝恢复（409 INPUT_CHANGED）
- 运行中/排队中任务可取消（test_cancel_running_job / test_cancel_queued_job_directly）
- 模型登记与下载、GLB 魔数校验、版本 v1 记录（test_models_registered_and_downloadable）
- 同项目并发任务拒绝（409）

## 3. 端到端测试（真实 COLMAP 3.13 + GPU + south-building 数据集）

**任务 #7**（40 张 7MP 真实照片、sequential 匹配、standard 预设、稠密+网格开启）：

| 阶段 | 结果 | 耗时 |
| --- | --- | --- |
| IMAGE_VALIDATION | completed（40 张有效） | <1s |
| FEATURE_EXTRACTION | completed（GPU SIFT） | 6s |
| FEATURE_MATCHING | completed（sequential） | 4s |
| SPARSE_RECONSTRUCTION | completed（32/40 注册） | 48s |
| IMAGE_UNDISTORTION | completed | 3s |
| DENSE_RECONSTRUCTION | completed（GPU，与其他任务共享时） | 2030s |
| STEREO_FUSION | completed（390 万彩色点） | 231s |
| MESHING | completed（Poisson depth8 + 离群点过滤，51,474 顶点 / 98,129 面） | 1709s |
| EXPORTING | completed（4 产物） | 2s |

产物：sparse_pointcloud.ply（21,413 点）、dense_pointcloud.ply（105MB，3,907,705 彩色点）、
mesh.ply / model.glb（51,474 顶点 / 98,129 面）、job_manifest.json。
下载校验：GLB 魔数 `glTF`，1.91MB。模型版本 v1 记录正常。

**断点恢复实测**：任务先后经历 3 次失败（外部终止 poisson / 代码修复重启），
每次 resume 均正确跳过全部已完成阶段（"检查点有效，跳过"），未重复执行稠密重建（2030s 结果复用）。

**全量数据集**：south-building 128 张全量 SfM+MVS 手动管线验证通过（126 张注册、1249 万点融合）。

另：synthetic smoke-30 数据集对真实 COLMAP 匹配率不足（2/30 注册）——已定位为渲染纹理
跨视角一致性问题，保留用于 Mock 演示；phase 2 计划修复后作为离线可复现的重建测试集。

## 3.1 前端 GUI 实测（Playwright + Chromium headless）

```text
PASS 登录页加载 / 注册成功提示 / 登录后进入项目列表 / 新建项目出现
PASS 项目详情加载 / 图片上传与缩略图 / 质量检查完成 / 任务配置对话框
PASS 模型查看页加载 / 风格切换完成（写实→莫兰迪→线框→点云截图存档）
通过 10 项 / 失败 0 项
```

截图：`data/gui-screenshots/01~05*.png`（项目详情含缩略图与质量标签；三维查看器
真实渲染重建网格并完成 4 种风格切换；信息面板显示顶点/面数/包围盒/版本）。

## 4. 前端

```text
$ cd frontend && npm run build
✓ built in 6.58s（1699 modules）
```

浏览器 GUI 实测项：注册/登录、新建项目、批量上传、缩略图显示、质量检查、提交任务、
任务页阶段时间线/日志实时刷新、取消按钮、模型列表、三维查看器（旋转/缩放/风格切换/下载）。

## 5. 已知限制

- 合成数据集（sample-data/smoke-30）对真实 COLMAP 的 SfM 匹配率不足（注册 2/30），
  仅用于 Mock 模式演示与前端联调；真实重建测试使用 south-building 数据集。
- 第一阶段未实现：任务 WebSocket 推送（2s 轮询代替）、分片断点续传、追加照片/版本更新（P2）。
