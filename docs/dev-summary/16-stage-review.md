# 阶段整理报告：已完成、优化效果与待办

**范围：** 从项目初始化到 Bridge 提速、R01 顶部网格与原色贴图，以及后续局部平台试验。阶段编号沿用[原始开发指南 §12](../../DEVELOPMENT_GUIDE.md#12-开发阶段与里程碑)。本页汇总已有证据，不是一次新的系统或浏览器验收。运行版本、模型 ID 和启动方式以[版本管理](../版本管理.md)为准。

## 1. 原计划完成情况

| 阶段 | 状态 | 已完成 / 已验证 | 未完成或限制 |
| --- | --- | --- | --- |
| 0 环境摸底 | 核心完成 | 核对 A800、CUDA COLMAP、存储与授权 SSH 转发 | 恢复旧环境参考版本不一定与运行进程完全相同；见[启动说明](../三个演示版本启动说明.txt) |
| 1 项目骨架与接口 | 核心完成 | Vue、FastAPI、SQLite/Alembic、API 契约、Mock 管线 | 最终离线演示包尚未定稿 |
| 2 用户、项目与图片管理 | 核心完成 | 认证、数据隔离、批量上传、预览、删除、质量检查；浏览器上传 100 张、920,566,231 字节原片 | 不将单文件整传重试当成分片断点续传 |
| 3 真实重建引擎 | 核心完成 | 实测 40、100 张真实照片从上传到模型发布；128 张独立运行验证到融合，未验证网格 | 不同场景的完整度与朝向仍需单独评价 |
| 4 模型管理和 Web 展示 | 核心完成 | PLY、GLB、照片贴图、查看/下载校验；实验版名称、父版本和范围可追溯 | 新候选的几何与外观保留独立人工验收，不等同于下载哈希通过 |
| 5 可靠性与恢复 | 核心完成 | 阶段检查点和哈希、取消、Worker 强杀识别、显式恢复、SQLite 备份恢复演练 | Worker 强杀后仍需确认遗留引擎已退出再恢复；见[正式验收](../phase2-acceptance.md) |
| 6 性能与质量优化 | 核心完成，部分补测未做 | 同条件双卡提速、2000/3000 px 对照、组合方案完整冷跑、顶部网格参数对照 | Bridge 固定 2000 px 单/双卡对照尚缺双卡实测；部分组合仍是估算，完整组合跑每组只有一次 |
| 7 云端任务与版本更新 | 核心完成 | 真实追加 40→50 张后全量重建发布新版本；隔离回归验证冻结输入清单归因、发布失败不破坏旧版、恢复只生成一个正确的后继版本 | 追加对照同时改了分辨率，不能单独归因质量提升；E8 真实多项目排队对照未做 |
| 8 局部重建可行性 | 局部平台部分验收 | [#24 提取与局部点云](18-local-platform-review.md)经独立 HTTP/RQ 运行，用户反馈网页操作无问题；早期单独裁剪难辨是另一项观感反馈 | 自动化真实浏览器闭环、任意新照片注册、网格共边界拼接尚未验收；离线新照收益证据不一致。 |
| 9 系统测试、文档与答辩 | 部分完成 | 已有[图文用户手册](../user-manual.md)、[课程报告正文初稿](../course-report.md)、[报告提纲](../course-report-outline.md)、实验资料、部署说明和离线模型样例 | 补封面与分工、报告最终定稿、PPT、便携演示包、断网彩排及交付版本统一回归 |

## 2. 优化做了什么，效果怎么样？

| 优化 / 决策 | 实测或反馈 | 当前结论 |
| --- | --- | --- |
| 双 GPU | South 40 张、固定 2000 px：稠密 1727.32→868.19 s，约 **1.99 倍**；整任务 32.39→18.13 分钟 | 明确提速；结果只适用于这组固定分辨率对照。见[GPU 实验矩阵](11-gpu-resolution-benchmark.md) |
| 2000→3000 px | 统一口径 12 视角复测：两个场景的网格投影覆盖和融合点数增加，照片贴图面率下降 | 不称全面提升；保留速度与细节权衡。Bridge 历史 83.91→75.89 分钟同时改变卡数和分辨率，不能当成纯双卡收益 |
| **C01 组合提速**：3 次 PatchMatch、融合 16 线程、贴图从 0.125 档开始 | 相同 Bridge 100 张、双卡 3000 px、两组各全新运行一次：**73.70→48.00 分钟（−34.9%）**；照片贴图面率 89.62%→91.62%，网格投影覆盖 96.55%→94.19%，近点支撑 91.12%→90.26% | 保留 **#22** 候选，基线 **#17**。用户目测主体结构差别不大；覆盖回退仍照实记录，组合收益不拆分归因，不切换 `18082`。见[完整冷跑报告](14-combined-cold-run.md) |
| **R01 顶部缺面**：固定 C01 点云，Poisson trim 10→9，重贴图 | 上部网格投影近点像素 65,447→91,060（+39.1%）；新增像素中无近点 4.7%，全模型贴图面率 91.62%→87.83%；用户反馈当前效果不错 | `18469` 项目 #1 **#23 网格 / #24 贴图 GLB**；仅重做网格和贴图，不是新冷跑。更激进的 trim=8 有更明显外插，未发布；未拍到的屋面不当成已修复。见[顶部排查](15-bridge-roof-audit.md) |
| **原色贴图 + 预览曝光** | 用户目测比「写实」更亮、观感更好；原始 GLB 下载哈希不变 | 推荐该组模型展示时优先试原色贴图；不是定量测光、纹理重烘焙或几何改进。默认模式未改 |
| 前沿重建与未观测表面补全 | 已做[方案调研](12-reconstruction-frontier.md)，未用学习模型替换主链，也无屋顶真值证明猜测补全可靠 | 需要先在隔离环境用留出视角验证；推测几何与实测模型分开。课程交付不依赖此项 |

## 3. 下一步优先级

| 优先级 | 工作 | 完成判据 |
| --- | --- | --- |
| 高 | 阶段 9 最终交付 | 把已验收的代码与模型版本写进正文；填封面与分工，完成 PPT 和离线包；组员按手册断网走一遍 |
| 高 | 确定演示采用哪套配置 | 用 #17、#22、#24 同角度核对屋檐、窗框、缺面与暗部；区分快速重建候选、上部网格候选、仅预览改动。经小组认可后再决定是否切换；不默认覆盖 `18082` |
| 中 | GPU 性能补测、E8 队列验收 | 按[建议补测](建议补测.md)先做 Bridge 2000 px 固定条件双卡，再补重复次数和资源采样；另用隔离 Redis/RQ 验证多项目排队与失败后的续跑 |
| 中 | 局部操作补验 | [已补父版定位、隔离回归与 #28 评分](19-viewer-and-platform-holdout.md)；新版真实浏览器提交待验；网格融合、未观测屋面补全不阻塞答辩 |

## 4. 验收记录表（可继续填写）

| 日期 / 验收人 | 服务 / 版本 / 模型 ID | 操作、视角与风格 | 实测或目测结果 | 证据链接 | 结论与后续 |
| --- | --- | --- | --- | --- | --- |
| 2026-11-19 / 成员A | `18082` 阶段8隔离平台 | 真实浏览器：注册→上传→重建→查看→下载哈希校验 | 通过（真实浏览器 GUI 断言 + 截图；隔离 HTTP/RQ 回归） | [phase2-acceptance.md](../phase2-acceptance.md) | 通过；遗留项转入集成验收 |
| 2026-11-20 / 成员A | 隔离回归环境 | phase2 验收缺陷修复后的隔离复测（冻结输入归因、发布失败不破坏旧版、恢复生成唯一后继） | 通过 | [03-quality-round.md](03-quality-round.md) | 通过 |
| 2026-11-20 / 成员A | C01 组合提速（3 次 PatchMatch、融合 16 线程、贴图 0.125 档） | Bridge 100 张、双卡 3000 px，两组各全新冷跑一次 | 完整冷跑 **73.70→48.00 分钟（−34.9%）**；贴图面率 89.62%→91.62%，网格投影覆盖 96.55%→94.19%（如实记录回退） | [14-combined-cold-run.md](14-combined-cold-run.md)、[13-quality-speed-optimization-results.md](13-quality-speed-optimization-results.md) | 保留 #22 候选；收益不拆分归因 |
| 2026-11-21 / 成员A、成员B | `18469` 项目 #1 #23 网格 / #24 贴图 GLB（R01） | 顶部单色／线框复核 + 同视角写实/原色对比；目测 | 上部网格投影近点像素 65,447→91,060（**+39.1%**）；新增像素中无近点 4.7%；贴图面率 91.62%→87.83%；成员B反馈顶部还原与原色观感良好 | [15-bridge-roof-audit.md](15-bridge-roof-audit.md) | 保留 #24 候选；未拍到屋面不当成已修复 |
| 2026-11-21 / 成员B | `18469` #24 原色贴图模式 | 查看器写实↔原色切换目测；原始 GLB 哈希复算 | 原色观感优于写实；下载哈希不变 | [15-bridge-roof-audit.md](15-bridge-roof-audit.md) | 演示优先原色；默认模式未改 |
| 2026-11-21 / 成员A | `18479` 独立局部平台 | #24 区域提取 + 留出照片局部点云（独立 HTTP/RQ） | 用户网页操作无问题；[E7 平台留出评分](19-viewer-and-platform-holdout.md)通过 | [18-local-platform-review.md](18-local-platform-review.md)、[19-viewer-and-platform-holdout.md](19-viewer-and-platform-holdout.md) | 不替换主线模型 |
| 2026-11-21 / 成员A | 集成候选（隔离环境，`acceptance_env.sh` 管理） | **双场景 3+3 冷跑矩阵**：Bridge 100 张 3000 px、South 36 张 2000 px，dual/single 各 3 轮全新运行 | Bridge 稠密中位 6277.83→3158.32 s = **1.988×**；South 1539.66→783.14 s = **1.966×**（South 首轮单卡臂被残留双卡 Worker 污染，已按协议作废并重测） | [bridge-cold-matrix.json](../integrated-acceptance-evidence/bridge-cold-matrix.json)、[bridge-cold-matrix-single.json](../integrated-acceptance-evidence/bridge-cold-matrix-single.json)、[south-cold-matrix.json](../integrated-acceptance-evidence/south-cold-matrix.json)、[south-cold-matrix-single-clean.json](../integrated-acceptance-evidence/south-cold-matrix-single-clean.json) | 通过 ≥1.7× 门槛；污染事故促成本轮 Worker 指纹管理脚本 |
| 2026-11-21 / 成员A | 集成候选 D 链（D1–D4，两场景） | Bridge 3 张 / South 4 张留出照片：注册→局部融合→网格补丁→顶点色 | D1/D2 通过：父模型与父点云 SHA-256 字节级不变、新视角注册 0 失败、父位姿不变；D3/D4 在两场景均**拒绝发布**（父网格天然洞与补点价值区重合，边界环对应不适定） | [d-chain-results.json](../integrated-acceptance-evidence/d-chain-results.json)、[20-integrated-system.md](20-integrated-system.md) | 诚实拒绝按设计生效；重叠带重网格化列入后续工作 |
| 2026-11-21 / 成员A | speed 画像 + NFS scratch 对照实验 | speed 冷跑 1 轮有效（另 2 轮因双实例竞工作废，接受现状）；同 job 迁移 `/tmp` 本地盘 scratch 重跑稠密 | speed 稠密 1960.2 s ≈ C01 的 1965.4 s（差 0.3%），整轮 50.7 分钟 ≈ 48 分钟；NFS scratch 使单次稠密迭代 4.8 s→16 s（**3.4×**） | [bridge-cold-matrix-speed.json](../integrated-acceptance-evidence/bridge-cold-matrix-speed.json)、[bridge-speed-tmp-scratch.json](../integrated-acceptance-evidence/bridge-speed-tmp-scratch.json) | speed 画像保留；`SCRATCH_DIR` 本地盘列为部署硬性要求 |
| 2026-11-22 / 成员A | Bridge 验收环境 | `scripts/acceptance_env.sh` stop→start→status 全闭环；真实 PID 捕获、陈旧 PID 指纹确认 | 通过；历史进程（18082/18469 Worker、历史 Redis）零误伤 | [20-integrated-system.md](20-integrated-system.md)、脚本内注释 | 本轮事故（包装 PID、残留 Worker、NFS scratch）统一收口 |
| 2026-11-22 / 成员A | 常驻集成候选站（`18507`，真实 Chromium） | 注册→上传 South 36 张→standard 全跑（3000 px 双卡，约 23 分钟）→写实/原色切换→框选区域→区域提取→下载哈希→局部重建（4 张留出，真实 UI 提交） | 主流程全部通过：区域提取 SUCCEEDED、region GLB 下载哈希 `45f5a51f…`；局部任务 #3 由守卫诚实拒绝（盲拖选区无留出照片增量点）；**发现并修复真实前端缺陷**（general 模式照片选择器全部禁用，b5b70db） | [browser-e2e/](../integrated-acceptance-evidence/browser-e2e/)（16 张截图 + manifest.json） | 通过（主流程）；局部守卫按设计工作 |
| 2026-11-22 / 成员A | Bridge 验收环境（D3 v2' 重叠拼接） | 继承 #28 验证选区 + job-10 融合点云 → 网格补丁 | **首次真实发布**：模型 #50（486,104 面，20.2 MB），核心盒外 444,394 面逐字保留、非流形 0、缝宽中位 0.0126 ≤ 容差 0.081，下载哈希 `ce52b9e8…` 复核一致；South 两选区量化诚实拒绝（补丁壳在父洞区因 trim 收缩） | [mesh-patch-results.json](../integrated-acceptance-evidence/mesh-patch-results.json)、[20-integrated-system.md](20-integrated-system.md) | D3 机制可用；诚实边界收窄为 trim-密度权衡 |

使用时将**实测、估算、用户目测、未验**分开写。示例中的照片贴图面率和投影覆盖量化的是不同现象，都不是几何真值。

#24 后续的两项局部操作可在[独立平台候选](18-local-platform-review.md)演示；它们不替换主线模型，也不代表贴图网格拼接通过。
