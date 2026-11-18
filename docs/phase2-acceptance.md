# 第二阶段验收与交付

## 已核实的能力

- 在线任务保留 CUDA COLMAP 3.13 重建；配置 `COLMAP_TEXTURER_BIN` 后，独立的 COLMAP 4.2.1 CPU 阶段生成内嵌照片纹理 GLB，保留源 PLY 和顶点色 GLB。贴图失败有阶段说明；建筑竖直轴证据不足或超时则保留原坐标，提供手动校正。
- 检查点绑定输入、引擎、参数和上游产物，校验 SHA-256，并检查逐图深度图和最终产物。发布时，模型、版本及任务成功状态在同一数据库事务提交。
- 浏览器上传按最多 12 张一批顺序提交，显示字节加权进度，可单独重试失败文件。Chromium fixture 测试验证了部分失败、重试、贴图回退通知、桌面和移动画布像素及视角/风格操作。
- 真实 Redis/RQ 故障注入：无输出且忽略 SIGTERM 的测试进程在取消后退出（2.593 秒）；强杀 Worker 后替换 Worker 在 1.892 秒将任务标为 `FAILED / WORKER_LOST`。测试先清理遗留子进程，再显式恢复，复用校验和特征提取检查点并发布模型。强杀不保证自动清理引擎进程。证据：[worker-fault-drill.json](../tests/worker-fault-drill.json)。
- SQLite WAL 隔离演练验证了已提交行被恢复、未提交行不进入备份、迁移版本不变及备份篡改被拒绝。两端完整性检查为 `ok`，文件哈希一致。证据：[sqlite-restore.json](phase2-evidence/sqlite-restore.json)；复跑 `backend/.venv/bin/python scripts/drill_sqlite_restore.py`（参数见 `--help`）。依赖版本、启动、备份与离线演示见[部署手册](deployment.md)。

## 正式数据与回归

- [ETH3D Bridge 原始 DSLR JPEG](https://www.eth3d.net/data/bridge_dslr_jpg.7z)，按文件名排序取前 100 张，不重新编码。共 **920,566,231 字节**，每张超过 4 MB；[原片哈希清单](phase2-evidence/bridge100.sha256)。验收 GPU 稠密处理设为最大边 1000 像素；这是计算参数，不改变上传原片。试验集为 South Building 前 40 张。
- [40 张试验集日志](phase2-evidence/pilot.log)：真实 API、Redis/RQ、GPU 重建、CPU 贴图和下载成功；产物含 17,674 个三角面。
- [正式集首次试跑](phase2-evidence/formal.log)：100 张均上传、完成几何重建，但图集 32768 × 11103，严格贴图门槛不通过。改为读取 PNG 尺寸头、超过 8192 边长自动降一档重烘焙；不关闭 Pillow 安全限制。[尺寸重试证据](phase2-evidence/atlas-retry.log)记录同一正式网格由 32768 × 11103、16384 × 5676 降到 8192 × 2974。
- **正式浏览器复验通过。** 隔离环境任务 #3 从页面上传 100 张原片，提交 `standard` 任务，经真实 Redis/RQ Worker 运行，随后在页面查看并下载照片贴图 GLB。任务执行耗时 **46 分 16 秒**，其中稠密重建 31 分 1 秒，贴图阶段 5 分 10 秒（含图集重试与朝向估算）。网格有 **27,041 面**，其中 **26,741 面（98.89%，按面数）**获得照片纹理；图集 **8192 × 2847**，GLB **14,217,984 字节**。朝向估算超时，下载文件保留原坐标；未认定自动正立。
- 证据：[浏览器结果](phase2-evidence/bridge-browser.log)、[任务清单](phase2-evidence/bridge-job3.json)、[上传](phase2-evidence/live-upload.png)、[任务](phase2-evidence/live-task.png)、[查看器](phase2-evidence/live-viewer.png)。模型样例保存在 `data/phase2-bridge/model_photo.glb`（运行数据，不入 Git），[逐阶段验收 JSON](phase2-evidence/bridge-verification.json)记录 100/100 张注册、56,714 个稀疏点和 1.034 px 平均重投影误差。稠密点云含 836,113 点。查看、下载及服务端产物的 SHA-256 一致，为 `9d091cba74fa4375bf4c03ab74cfe3bb6c173aa3ed2b0ef6440f9e9045ad25ec`。
- [30 秒间隔资源采样](phase2-evidence/bridge-resource-samples.log)从稠密重建中途开始：观测到 COLMAP 进程显存 752 MiB、单子进程 RSS 12,202,032 KiB、任务工作目录 4,816,977,478 字节的采样最大值；不代表全程峰值。
- 后端 `cd backend && .venv/bin/pytest -q --disable-warnings`：73 项通过。前端 Node 22 环境 `npm run build`：通过。

## 同条件 Poisson 对照

使用试验集 #7 保留的同一份过滤融合点云、同一 COLMAP 3.13 二进制、同一主机、深度 8，每种线程设置运行一次。1 线程 **14.46 秒**，4 线程 **12.48 秒**；自动线程 **90 秒超时**，不能给出精确加速倍数。1/4 线程分别产生 98,128 / 98,134 面，表面积约 37.36678 / 37.36683，双向最近顶点距离 P95 均约 0.0000362（模型坐标单位，不是米，也不是参考真值误差）。因此采用固定 4 线程，避免本环境自动线程耗时不稳定；此结果不代表所有硬件或所有输入的最优线程数。

正式 ETH3D Bridge 同一融合点云也做了单次对照：1 线程 **4.05 秒**，4 线程 **3.38 秒**，自动线程 **90 秒超时**。1/4 线程分别产生 27,479 / 27,477 面，双向最近顶点距离 P95 为 0.000372 / 0.000377 模型坐标单位。该对照与正式 GPU 验收并行，同组按顺序运行；数值是该环境的观测，不是资源独占下的多轮统计。[原始结果 JSON](phase2-evidence/poisson-bridge.json)。

复跑：`backend/.venv/bin/python -I scripts/benchmark_poisson.py /path/to/colmap /path/to/fused-filtered.ply /path/to/new-result-dir`。

## 同视角查看器对照

在 Chromium 中用任务 #3 的真实 GLB 和相同默认取景比较[顶点色版](phase2-evidence/vertex-home.png)和[照片贴图版](phase2-evidence/photo-home.png)：二者均为 27,041 面，包围盒一致，单色前景掩膜重叠率 100%。照片贴图版的[拖动旋转](phase2-evidence/photo-orbit.png)、[滚轮缩放](phase2-evidence/photo-zoom.png)与重置均通过像素断言，未发现页面或控制台错误。详见[断言和文件哈希](phase2-evidence/matched-view.json)。包含原照 `DSC_1174.JPG` 的对照图在 `data/phase2-matched-view/matched-view.png`，原照是外观参考，**与两张模型截图没有做相机位姿对齐**。

与正式浏览器全链路测试不同，此对照由测试夹具提供真实 GLB，不依赖线上服务。可从仓库根目录复跑：

```bash
backend/.venv/bin/python scripts/phase2_matched_view_test.py \
  --job-dir data/phase2-bridge --dist frontend/dist \
  --output data/phase2-matched-view-rerun
```

## 交付核对

| 计划项 | 对应证据 | 结论 |
| --- | --- | --- |
| 在线贴图、朝向及降级 | 正式浏览器任务 #3、纹理清单、同视角截图；独立建筑示例验证正立导出 | 在线贴图通过；正式 Bridge 自动朝向超时，手动校正可用 |
| 可靠性 | SHA-256/阶段回归、真实 Redis/RQ 故障演练 | 取消、识别中断、显式恢复通过；强杀后的遗留子进程需清理 |
| 正式数据与性能 | 100 张原片哈希、浏览器日志、模型 SHA-256、Poisson 对照及资源采样 | 全链路与单次性能对照通过；资源非全程采样 |
| 部署与交付 | 依赖锁、部署手册、SQLite 恢复、`data/phase2-bridge/` 离线模型与校验清单 | 交付物已归档；未切换既有服务 |

## 尚未验收的范围

不将贴图覆盖率当成表面积覆盖率、几何精度或缺面修复；没有与原照严格匹配位姿的人工质量评分，也没有从上传起覆盖全过程的资源峰值。强杀后须确认遗留引擎已退出再恢复。正式跑在隔离的 API、Redis、SQLite、Worker 上；未切换现有演示的后端与 Worker。追加照片版本更新、局部融合、分布式调度及公网部署不在本阶段。

## 第二轮质量修复（2026-11-19 追加）

针对"建筑物大量空洞"的反馈完成根因诊断与修复：稠密重建分辨率 1000px→2000px、`standard` 预设 Poisson depth 8→10（与开发指南预设表一致，含同视角 depth×trim 数据支撑），并以 2000px + depth10 通过应用内完整重跑发布新模型（Bridge #29 / South #34，e2e_user 项目 2/1）。同视角诊断、参数对照、源相机前后对比渲染与浏览器实测见[建筑缺面排查](geometry-completeness.md)。原验收中的 GLB/贴图指标仍以任务 #3 为准；#6/#7 是质量修复轮的正式产物。
