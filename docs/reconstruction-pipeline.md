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
| 稀疏重建（32/40 注册） | ~60s |
| 去畸变 | ~2s |
| 稠密重建（32 视图，GPU） | ~28 min（与另一 128 图任务共享 GPU 时） |
| 点云融合（390 万点） | ~4 min |
| Poisson depth10 | ~15-25 min（CPU 密集） |
