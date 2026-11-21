# 重建管线设计

## 1. 系统阶段 ↔ COLMAP 命令映射

| 系统阶段 | COLMAP 命令 | 关键参数 | 输出检查 | 权重 |
| --- | --- | --- | --- | --- |
| IMAGE_VALIDATION | （管线内实现） | 图片复制/命名 `img_NNNN_原名.ext` | validated.json，≥3 张 | 2 |
| FEATURE_EXTRACTION | `feature_extractor` | SIMPLE_RADIAL, max_features 8192, use_gpu | database.db | 13 |
| FEATURE_MATCHING | `exhaustive_matcher` / `sequential_matcher` | auto→按数据集选（无序=穷举，视频帧=顺序） | database.db | 15 |
| SPARSE_RECONSTRUCTION | `mapper` | output_path=sparse | sparse/*/cameras.bin（复制到检查点） | 15 |
| IMAGE_UNDISTORTION | `image_undistorter` | output_type COLMAP | dense/sparse/cameras.bin（3.13 布局） | 5 |
| DENSE_RECONSTRUCTION | `patch_match_stereo` | geom_consistency true，**需 CUDA** | stereo/depth_maps.bin | 25 |
| STEREO_FUSION | `stereo_fusion` | input_type geometric | fused.ply | 10 |
| MESHING | `poisson_mesher` | depth: fast=8 / standard=10 / high=12 | meshed-poisson.ply | 10 |
| EXPORTING | （管线内实现） | 着色/GLB/预览图 | exported.json | 5 |

## 2. 版本兼容（3.7 ↔ 3.13 实测）

- GPU 选项：`SiftExtraction.use_gpu`(3.7) ↔ `FeatureExtraction.use_gpu`(3.10+)；SIFT 算法参数前缀不变；
- 稠密输出布局：3.7 扁平 `dense/cameras.bin` ↔ 3.13 `dense/sparse/cameras.bin`；
- 符号链接图片在 3.13 被解析为规范路径 → 必须**复制**图片；
- `mapper` 产出多子模型时取 `sparse/0`（自动探测存在 cameras.bin 的子目录）。

## 3. 降级策略

- 无 CUDA 构建（apt 3.7）：跳过 UNDISTORTION/DENSE/FUSION/MESHING，事件记录"未启用（或无 CUDA）"，
  仍产出稀疏点云 → 可查看/下载；警告写入任务事件。
- COLMAP 缺失：创建任务时即报 COLMAP_UNAVAILABLE（或使用 mock 模式）。

## 4. 导出产物（EXPORTING）

| 产物 | 来源 | 用途 |
| --- | --- | --- |
| sparse_pointcloud.ply | 稀疏模型 points3D（含颜色） | 稀疏结构展示 |
| dense_pointcloud.ply | fused.ply 复制 | 稠密点云展示/预览图 |
| dense_pointcloud.glb | PLY→glTF POINTS | Web 展示（大点云降采样 ≤300 万） |
| mesh.ply | Poisson 网格 + 稠密点云最近邻着色 | 网格展示 |
| model.glb | mesh→GLB（顶点色 COLOR_0） | Three.js 主展示模型 |
| preview.png | 点云 matplotlib 降采样渲染 | 模型列表预览 |
| job_manifest.json | 任务级元数据 | 追溯（输入哈希/配置/输出清单） |

## 5. 断点恢复

- 检查点目录 `checkpoints/{STAGE}/`：阶段产物 + manifest.json（params_sha / inputs_sha / 文件 sha256）；
- `inputs_sha = sha256({input_manifest, config})`：图片清单或配置变化 → 全部检查点失效；
- resume API 先比对当前项目图片 checksum 清单与任务 input_manifest，不一致返回 409 INPUT_CHANGED；
- Worker 重新执行时逐阶段 `stage_checkpoint_valid()` 校验，有效则记 skipped 事件并跳过。

## 6. 取消

- API 设 cancel_requested + 状态 CANCEL_REQUESTED；
- Worker 在阶段边界 `check_cancel()`；子命令执行期间由 Adapter 每 ~1s 轮询一次，
  触发即向整个进程组发 SIGTERM（COLMAP 多线程子命令可安全终止），抛 StageCancelled；
- 终态 CANCELLED（保留日志与已完成阶段检查点，可 resume）。

## 7. 实测性能参考（A800 GPU / 56 核，south-building 子集 40 张 7MP）

| 阶段 | 耗时 |
| --- | --- |
| 特征提取（GPU） | ~7s |
| 顺序匹配（GPU） | ~10s |
| 稀疏重建（最终检查点 40/40 注册） | ~60s |
| 去畸变 | ~2s |
| 稠密重建（GPU） | 首次 2030s（与另一 128 图任务共享 GPU 时） |
| 点云融合（390 万点） | ~4 min |
| Poisson depth 8（最终成功配置） | 1709s（第一阶段历史运行） |

第二阶段采用固定 4 线程；正式集的可复现对照见[验收报告](phase2-acceptance.md#同条件-poisson-对照)。


## 5. 集成系统：通用配置冻结与质量门槛（2026-11-21 追加）

总规范见[集成系统验收标准](集成系统验收标准.md)。本节记录管线行为的变化；历史阶段表和版本兼容说明仍然有效。

### 5.1 配置冻结（阶段 A1）

- 任务创建时 `job_service.create_job` 经 `reconstruction/profiles.py` 一次性解析并冻结：preset 画像参数、最终匹配策略与 auto 决策理由、稠密最大边、PatchMatch 迭代、融合线程、Poisson 深度/线程/trim、贴图起始档、GPU 索引与二进制路径。执行与断点恢复只读 `config["resolved"]`，环境变量漂移不再改变行为；无 `resolved` 的旧任务回退历史行为。
- 画像：`fast/standard/high` 语义不变；新增通用 `speed` 画像（PatchMatch 3 次、融合 16 线程、贴图 0.125 起，源自组合提速实测，非场景专用）。
- 稠密任务的提交门槛：CUDA COLMAP 必须可用（否则 `CUDA_UNAVAILABLE`）；`COLMAP_GPU_INDEX` 请求的每张卡必须真实可见（否则 `GPU_INDEX_UNAVAILABLE`）。不再静默把稠密任务降级为稀疏成功。
- 提交预检 `_preflight_images` 记录文件缺失/分辨率/质量汇总；视角重叠如实记 `unknown_until_registration`（重叠只能在注册后判定）。
- 发布清单新增：`engine.binary_sha256`（引擎二进制）、`resolved_params.matcher_decision/poisson_threads/poisson_trim`、`preflight`、`capability`、`mesh_quality`。

### 5.2 质量管线（阶段 A2）

- MESHING 产出面支撑代理指标（`mesh_quality.py`：面质心到融合点云最近距离，半径 = 3× 中位最近邻间距），按 display_up 分 bottom/middle/top 带。
- 上部带支撑 <0.7 且 trim>8 时自动生成 trim-1 候选；仅当"支撑更高且无支撑比例增长 ≤3pp"才采用（`choose_mesh_candidate`）。裁定理由与两个候选指标进入 MESHING manifest 与发布清单。
- 相机最高高度未进入点云上部 20% 高度带 → 任务警告"顶部视角不足，顶部重建结果不可验证"。未观测表面不会被 trim 或外插冒充为已还原。
- 贴图阶段元数据新增 `registered_photos` 与 `implementation.binary_sha256`；失败仍明确 fallback，不冒充完整。

### 5.3 局部更新（阶段 D）

| 模式 | 输入 | 阶段 | 输出与门槛 |
| --- | --- | --- | --- |
| `local_update`（历史） | 已有网格 + 选区 | LOCAL_UPDATE | 独立裁剪 GLB；报告裁剪语义/父哈希/开放边界/贴图资产 |
| `local_reconstruct`（历史种子模式） | 固定种子 + 恰好 3 张 | 校验→稠密→融合 | 点云候选；保留为历史兼容，不是通用主路径 |
| `local_general`（D1/D2） | 任意 1–32 张新增照片 + 选区 | IMAGE_VALIDATION→REGISTER_NEW_VIEWS→IMAGE_UNDISTORTION→DENSE_RECONSTRUCTION→STEREO_FUSION→EXPORTING | 注册逐张报告（0 张即失败；父位姿逐字保持）；点云候选，父记录前缀逐字节不变 |
| `local_mesh`（D3/D4） | 局部点云候选 | IMAGE_VALIDATION→LOCAL_MESH_CUT→LOCAL_MESHING→LOCAL_STITCH→VALIDATION→EXPORTING | 补丁 GLB；父侧 UV/材质字节保留，补丁为融合色顶点；不变量（区域外面逐字保留、非流形=0、缝隙≤3×中位边长）不过则只写诊断报告 |

注册复用 COLMAP `feature_extractor` + `matches_importer`（显式配对）+ `image_registrator`（3.13 提供）；稠密视角集 = 注册新视角 + 距选区最近 ≤16 张父视角。