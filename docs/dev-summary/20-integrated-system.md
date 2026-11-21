# 集成系统迭代：从 Bridge 特化到通用管线（阶段 A–G）

父轮次：[19 查看器与 #28 复验](19-viewer-and-platform-holdout.md)。本轮目标不是再做 #24 特化模型，而是把系统改成**任意照片集**都能走完统一流程：主重建（双卡/3000 px/profile 提速）、原色贴图、区域提取、任意新增照片局部注册与融合、网格补丁候选。证据样例（#29/#59/#22/#24/#27/#28）只作为回归参照。总规范见[集成系统验收标准](../集成系统验收标准.md)。

## 已实现（按阶段）

| 阶段 | 内容 | 关键实现 |
| --- | --- | --- |
| A1 配置冻结 | 任务创建时一次解析并冻结 preset/匹配/稠密/贴图/GPU 参数；auto 匹配按文件名连续性决策；提交预检（缺失文件、分辨率、质量汇总，重叠如实标 unknown）；双卡能力核查（无 CUDA 或请求索引不可见 → 提交即失败，不再静默降级） | `backend/app/reconstruction/profiles.py`、`job_service.create_job`、`PipelineContext` 优先读 resolved |
| A1 speed 画像 | C01 组合提速参数变成通用可选画像 `speed`（PatchMatch 3、融合 16 线程、贴图 0.125 起），不绑定场景 | `profiles.PROFILES` |
| A2 质量管线 | Poisson `trim` 可配置；MESHING 阶段自动生成面支撑/分区支撑代理指标，上部支撑不足时按预声明门槛生成 trim-1 候选并裁定；相机高度提示"顶部不可验证"；贴图记录注册照片数与工具二进制 SHA-256 | `mesh_quality.py`、`pipeline._stage_meshing`、`texturer` 元数据 |
| B 原色贴图 | 可用性以 GLB 实际内容为准（`inspect_glb`：内嵌纹理+UV），详情页显示贴图资产实测；点云/无纹理网格不再出现误导选项；EV 仍是预览 | `glb_assets.py`、`ModelDetailOut.texture_assets`、`photoColorAvailable` |
| C 区域提取 | 报告记录裁剪语义、父哈希、开放边界数、导出文件贴图资产；支持已保存选区复用；回父模型选区（前轮） | `local_region.py`、`modelApi.listRegions` |
| D1 注册 | 新 `local_general` 模式：1–32 张任意新增照片，`feature_extractor + matches_importer + image_registrator` 注册到父稀疏模型；父位姿逐字保持；逐张报告成功/失败；注册 0 张即失败 | `local_general.py`、adapter 新增两个命令封装 |
| D2 局部融合 | 视角集 = 注册新视角 + 距选区最近父视角（≤16）；支持点须在保护带核心盒、多视角深度一致、父距离窗内；父点记录逐字节前缀 | 同上（`_select_dense_views`、`_select_patch(min_votes)`） |
| D3 网格补丁 | 新 `local_mesh` 模式：父侧核心盒外面片保留（visual/UV/材质原样），补丁 Poisson + 顶点级盒切，边界环弧长对齐缝合；不变量（区域外面逐字保留、无非流形边）不过 → 只写诊断报告不发布 | `local_mesh_patch.py`、`POST /models/{id}/local-mesh` |
| D4 补丁着色 | 补丁顶点色取融合点云多视角颜色；父侧贴图原样保留；manifest 标注"顶点色补丁，非图集" | 同上 |
| E 前端与证据 | 通用局部模式照片选择（不再限定文件名/数量）、任务详情冻结配置/预检/GPU 核查卡片、local_general/local_mesh 阶段时间线与中文标签、点云候选一键生成补丁 | `ModelViewerView.vue`、`JobDetailView.vue`、`api/index.ts` |

## 验证（分层，不互相替代）

| 层 | 结果 |
| --- | --- |
| 后端 pytest | **175 项通过**（新增 profiles/网格质量/GLB 资产/通用局部/网格补丁 5 个测试文件） |
| 前端 | node 回归通过；生产构建通过 |
| Chromium 夹具 | `scripts/phase8_gui_test.py` 通过：含已保存选区复用、网格补丁提交、归档/点云/普通版本导航、下载哈希 |
| 真实 HTTP/队列/Worker | 隔离 mock 环境 `e2e_test.py --preset speed --matcher auto` 全链路通过；发布清单实测含冻结参数、`auto→sequential` 决策、预检 30 张、工具/引擎身份 |
| 真实 GPU | **未执行**。D1–D4 的真实照片注册、局部融合、网格补丁、留出深度尚未在真实场景跑通；这是集成候选发布前的必要验收 |

## 真实 GPU 验收（2026-11-21 追加）

证据：[d-chain-results.json](../integrated-acceptance-evidence/d-chain-results.json)（随冷跑矩阵持续更新）。

| 项 | Bridge（C01/#24 快照父版） | South（36 张全新跑 + 4 张留出） |
| --- | --- | --- |
| 主链路 | 父工件经哈希钉扎登记（操作员快照，非重跑） | **全新真实跑通过**（36 张、双 A800、2000 px、1,021,006 面、贴图 GLB） |
| D1 注册 | 3/3 注册，父位姿逐字不变 | 4/4 注册，父位姿逐字不变 |
| D2 局部融合 | 候选 4,258,724 点（+454），父记录逐字节前缀 | 候选 1,661,168 点（+32），父产物哈希不变 |
| D3 网格补丁 | 门槛诚实拒绝（父网格 15 万/30.8 万顶点在开放边界，环对应不适定） | 门槛诚实拒绝（大区切穿全局边界；连续立面区 D2 诚实拒绝——父覆盖已充分，无可加点） |
| D4 | 未触发（前提是已发布补丁） | 同左 |
| D3 v2' 重叠拼接（2026-11-22） | **通过并发布**：模型 #50（486,104 面 / 20.2 MB，下载哈希 `ce52b9e8…f7304`），核心盒外 444,394 面逐字保留、非流形 0、缝宽中位 0.0126 ≤ 容差 0.081 | 两选区量化诚实拒绝：紧致选区缝宽中位 0.0044 达标但父洞残留最大 0.1105；中央三分之一区补丁壳大面积收缩（中位 0.454）——trim-密度收缩 vs 推测面的根本权衡 |

真实验收期间发现并修复 6 个实现问题：`new_names` 未初始化、NFS 临时目录清理掩盖真实异常（两处）、COLMAP 选项前缀未随探测版本刷新、`local_general` Worker 分支缺 publish/return（误跑标准管线并误发布，已清理）、`_inside` 未导入、巨型天然洞环被误选（环选择加包围盒尺寸过滤）。另有一次**操作失误**：验收组装脚本曾把 alembic upgrade 指向默认历史库（22651a4c6e93 → head 的两个可加性迁移），已备份 `recon.db.pre-acceptance-audit-*.bak` 并核实 8 用户/13 项目/255 图数据完整；脚本已加环境栅栏。

**架构结论（D3）**：环-弧长缝合只在"父表面连续但补点无价值"处可行，而补点价值区恰是父网格天然洞区，边界环普遍不对应。通用 D3 需要重叠带约束重网格化方案——按验收标准不伪造成功，交付名保持"通用主重建 + 通用区域提取 + 局部点云候选版"。**D4 决策**：不补做照片重投影接缝混合（前提是已发布补丁，不存在）。

## 网格补丁 v2' 重叠拼接（2026-11-22）

证据：[mesh-patch-results.json](../integrated-acceptance-evidence/mesh-patch-results.json)。代码：`local_mesh_patch.py`（v2'）；单测 16 项（含顶点复制焊接与捏点环分解回归）。

方案演进（每一步都有实测记录）：

1. **v1 环-弧长缝合**：两场景拒绝。复盘另发现两处实现缺陷并修复——GLB 父网格顶点按面复制（UV 需要），索引级边界分析把共享边判成边界（父"边界环"仅 3 点，实为碎片）；顶点级盒切割产生捏点（一个顶点汇入 4/6/8 条边界边），简单环遍历失效。修复后父侧边界环从 3 点碎片恢复为 26,022 点完整锯齿环。
2. **v2 重叠带重网格化**："补丁与父侧都在外盒平面切割、两侧边界同源同位"的前提被实测**证伪**——Poisson 曲面在点云边缘因 trim 收缩，trim 0/2/4/7 全部一致，从不与切割平面形成完整同位边界（South 外盒源点 105,862、trim 0 时补丁壳 bbox 也只与外盒相交于零星条带）。
3. **v2' 重叠拼接（当前）**：放弃环对应。补丁 = 外盒内全部源点（含父点）Poisson 曲面（trim 7，与主管线同类插值）裁剪到核心盒；核心盒外父面逐字保留；核心盒内"贴近补丁壳（≤1.5× 中位边长）且远离补丁边缘（≥2×）"的父面由补丁取代——补丁边缘叠在父面上（重叠式，不挖壕沟）；发布门槛 = 核心盒外逐字 + 非流形 0 + 缝宽（补丁边缘到存活父面逐点距离）中位 ≤ 容差、最大 ≤ 2× 容差。

| 场景 | 结果 | 关键数字 |
| --- | --- | --- |
| Bridge 继承 #28 验证选区 | **通过并发布**（模型 #50：486,104 面、327,129 顶点、20.2 MB） | 核心盒外 444,394 面逐字保留；非流形 0；缝宽中位 0.0126 / 最大 0.0475 ≤ 容差 0.081；下载哈希 `ce52b9e8…f7304` 与 Worker 输出一致 |
| South 紧致选区 #2 | 量化拒绝 | 缝宽中位 0.0044 达标；最大 0.1105 超限——280 个边缘点集中于核心盒 +x 面条带，80% 距原始父面 < 0.05：补丁壳在父洞区因 trim 收缩，洞区残留缝隙 |
| South 中央三分之一 #1 | 量化拒绝 | 缝宽中位 0.454：该区域融合新增点占比极低（d-chain：1,495/41,028），补丁壳大面积收缩远离父面 |

**诚实边界**：Bridge 的发布证明重叠拼接机制本身可用；South 的拒绝把 v1 的"环对应不适定"收窄为可操作的因果——**trim-密度收缩与推测几何的根本权衡**（低 trim 填洞但引入推测面，高 trim 诚实但洞区残留缝隙）。这属于产品决策而非实现缺陷，如实记录，不阻塞交付。**D4 决策更新**：Bridge 补丁已发布，D4 顶点色随 v2' 直接实现（融合点云最近点上色），无需单独补做照片重投影混合。

## 真实 GPU 冷跑矩阵与 scratch 发现（2026-11-21 完成）

证据：[d-chain-results.json](../integrated-acceptance-evidence/d-chain-results.json)、[south-cold-matrix](../integrated-acceptance-evidence/south-cold-matrix.json)、[south-single-clean](../integrated-acceptance-evidence/south-cold-matrix-single-clean.json)、[bridge-cold-matrix](../integrated-acceptance-evidence/bridge-cold-matrix.json)、[bridge-single](../integrated-acceptance-evidence/bridge-cold-matrix-single.json)、[bridge-speed-tmp-scratch](../integrated-acceptance-evidence/bridge-speed-tmp-scratch.json)。

| 矩阵 | 双卡稠密中位 | 单卡稠密中位 | 加速比 | 判定 |
| --- | --- | --- | --- | --- |
| South 36 张 2000px（standard） | 783.1s（3/3） | 1539.7s（3/3 干净轮） | **1.97×** | 通过 ≥1.7× 门槛 |
| Bridge 100 张 3000px（standard） | 3158.3s（3/3） | 6277.8s（3/3） | **1.988×** | 通过 ≥1.7× 门槛 |

两个场景复现 1.97–1.99×，与历史 South 1.99× 一致——双卡加速结论现在是跨场景、多轮次的复现结果。

**speed 画像与 scratch 发现**：speed 双卡 3 轮在 NFS scratch 上未复现 48 分钟（单次稠密迭代 16s vs 历史 4.8s，3.4× 膨胀）；对照实验把 `SCRATCH_DIR` 指向本地盘后，稠密 **1960.2s ≈ C01 的 1965.4s**、整轮 **50.7 分钟 ≈ C01 的 48 分钟**——**NFS scratch I/O 是根因**（历史环境 scratch 全在 /tmp 本地盘）。部署要点：`SCRATCH_DIR` 必须指向本地盘；文件损坏类 slowdown 也应先查 scratch 位置。

**验收过程中的操作事故（全部如实记录）**：alembic upgrade 误指默认历史库（可加性迁移，已备份核实）；验收环境多 Worker 残留导致任务被错误配置的 Worker 抢占（ South 单卡臂与 speed 第 1 轮受影响，均已作废重测）；两个 speed 矩阵实例竞争 GPU（终止其一）；孤儿 patch_match 进程占用 GPU（已清理）。

诚实边界：诚实边界：speed 画像的速度/质量数字仍是 Bridge 证据，不是通用保证；网格补丁的父侧保留是面片级近似（非解析平面求交），补丁边界为三角形锯齿；种子三图模式保留为历史兼容路径，不再是通用主路径。历史站点（`18082`/`18469`/`18479`）未被本轮源码覆盖。
