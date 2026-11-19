# 开发总结 6：优化前基线指标（对照基准，2026-11-20 固化）

> 用途：阶段 6/7/8 与双 GPU 优化的**对照基准**。所有数字来自真实运行的任务 #6/#7（第二阶段质量修复轮产物），口径见各表注。
> 回滚点：git tag `baseline-pre-opt-20261120`（bundle 备份 `../backups/repo-baseline-pre-opt-20261120.bundle`，含全部历史）。
> 对照完成后，新结果记入 [07-optimization-results.md](07-optimization-results.md)。

## 1. 配置口径

| 项 | 值 |
| --- | --- |
| 稠密分辨率 | `COLMAP_DENSE_MAX_IMAGE_SIZE=2000` |
| 网格化 | Poisson depth 10（standard 预设），trim=COLMAP 默认 10，4 线程 |
| GPU | **单卡**（COLMAP 未设 gpu_index → 固定 device 0），A800 80GB ×2 中仅用 1 块 |
| 贴图 | COLMAP 4.2.1 CPU texturer，图集上限 8192 |
| 数据 | Bridge 100 张（6048×4032，920,566,231 字节）；South 40 张（3072×2304） |

## 2. 速度基线（单 GPU，任务事件表实测）

### Bridge 100 张（任务 #6，总计 5,031.5s ≈ 84.0 分钟）

| 阶段 | 耗时 | 占比 |
| --- | ---: | ---: |
| IMAGE_VALIDATION | 2.3s | <1% |
| FEATURE_EXTRACTION | 25.3s | 1% |
| FEATURE_MATCHING | 52.6s | 1% |
| SPARSE_RECONSTRUCTION | 451.6s | 9% |
| IMAGE_UNDISTORTION | 27.2s | 1% |
| **DENSE_RECONSTRUCTION** | **3,916.9s** | **77.8%** |
| STEREO_FUSION | 181.4s | 3.6% |
| MESHING | 14.5s | 0.3% |
| TEXTURING | 357.0s | 7.1% |
| EXPORTING | 2.7s | <1% |

### South 40 张（任务 #7，总计 1,950.9s ≈ 32.5 分钟）

| 阶段 | 耗时 |
| --- | ---: |
| IMAGE_VALIDATION | 0.2s |
| FEATURE_EXTRACTION | 2.6s |
| FEATURE_MATCHING | 3.6s |
| SPARSE_RECONSTRUCTION | 50.4s |
| IMAGE_UNDISTORTION | 3.5s |
| **DENSE_RECONSTRUCTION** | **1,727.4s（88.5%）** |
| STEREO_FUSION | 70.4s |
| MESHING | 25.8s |
| TEXTURING | 59.8s |
| EXPORTING | 3.2s |

结论：**稠密重建占 78–89% 总时长且只跑单卡**——双卡图片级并行的最大收益点。

## 3. GPU 基线

| 项 | 值 |
| --- | --- |
| 使用卡数 | 1（device 0）；GPU 1 全程空闲 |
| 稠密阶段显存峰值（2000px，观测） | ~1.0–1.4 GB / 80GB |
| 稠密阶段利用率 | GPU 0 ≈ 100% |

## 4. 质量基线

### 配准与点云

| 数据 | 注册图片 | 稠密点数（融合过滤后） |
| --- | ---: | ---: |
| Bridge #6 | 100/100 | 2,653,961 |
| South #7 | 40/40 | 1,979,897 |

（参考：1000px 旧基线的重投影误差 1.034px；#6/#7 稀疏参数相同。）

### 同视角覆盖（work-scale 2000，splat 半径 4；z-aware 面积加权）

| 指标 | Bridge #6 | South #7 |
| --- | ---: | ---: |
| 点云覆盖（均值） | 79.0% | 78.7% |
| 网格面积（面片口径，均值） | 92.5% | 65.8% |
| 网格点支撑 | 90.5% | 99.3% |
| 无支撑外插 | 9.5% | 0.66% |
| z-aware 支撑面积（16/12 相机 sweep，depth10） | 74.3% | 64.4% |
| z-aware 无支撑外插（sweep） | 11.1% | 1.7% |
| P95 面心-点距 | 0.0309 | 0.0150 |

### 模型产物

| 项 | Bridge #6（#29） | South #7（#34） |
| --- | ---: | ---: |
| 网格面数 | 360,593 | 1,016,913 |
| 贴图面覆盖 | 330,798 / 360,593（91.7%） | 969,759 / 1,016,913（95.4%） |
| 图集 | 8192×3977 | 8192×3335 |
| 照片 GLB | 30.4 MB | 51.1 MB |
| 任务工作目录 | 8.9 GB | 3.8 GB |

### 视觉对比

`data/phase2-quality-round/before-after-sheet.png` 与 `data/quality-check/`（模型 #29/#34 浏览器实测 PASS、下载哈希一致）为视觉基准；优化后同视角复测以本页数字为准。

## 5. 对照规则

1. 速度对照：同数据、同 2000px/depth10、仅 GPU 卡数不同；稠密阶段单独比，总时长一并报。
2. 质量对照：同口径复测（本页同表名指标），要求**支撑面积不降、无支撑外插不升、点云覆盖不降**；任何提升需同时报代价（耗时/体积）。
3. 资源：本次起全程采样（15s 间隔，从任务提交到导出完成），报 CPU/内存/显存/磁盘峰值。
4. 单次观测如实标注；尽量对稠密微基准（20 张子集）重复 3 次。
