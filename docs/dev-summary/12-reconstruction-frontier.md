# 开发总结 12：重建前沿调研与下一步决策边界

> 本页是调研和方案边界，不是实验报告。本文没有运行 PatchmatchNet、VGGT、MASt3R、2DGS 或 GOF，也没有把论文/项目的公开结果当成本仓库数据。当前批准计划仍以 **COLMAP 网格 + 照片贴图 GLB** 为交付主线；本页只给出可以排期、对照和否决的后续路线。
>
> **链接口径：** 下列链接只指向论文页面、官方文档、官方项目页或官方代码仓库；不链接模型权重、数据集压缩包或其他未授权下载。

## 1. 先给结论

1. **默认主线不变：COLMAP SfM → PatchMatchStereo → geometric StereoFusion → Poisson → 照片贴图。** 当前适配器明确开启几何一致性并使用 geometric fusion（见 [`backend/app/reconstruction/adapter.py`](../../backend/app/reconstruction/adapter.py)）；这与交付物的网格和贴图形态一致。
2. **COLMAP 选择/一致性是低风险的下一步。** 先做可审计的 source-view 数量、固定 source 列表、几何一致性开关和 fusion 过滤的 A/B；不要把一次覆盖率变化称作绝对精度提升。官方 FAQ 明确指出，减少分辨率或源图可提速，`stereo/patch-match.cfg` 可手工指定源图或改变自动源图数量；关闭 `geom_consistency` 时仍应保留过滤。 
3. **PatchmatchNet 是离线候选，不是当前替换。** 它能提供学习型深度/点云对照，但要解决 COLMAP 相机坐标、尺度、源视图选择、深度融合和网格/贴图交付的适配问题；论文基准上的速度/内存数字不能移植为本系统数字。
4. **VGGT 与 MASt3R 更适合“相机/几何先验或补充对照”，不应直接宣称可替代网格级 COLMAP。** 它们的输出（相机、深度、point map、匹配或 tracks）必须经过尺度、坐标、可见性和一致性验收。当前质量不降门槛下，先离线导出并与冻结的 COLMAP 结果比较，不接入 Worker 默认路径。
5. **2DGS/GOF 适合另一个表示路线，不是当前 GLB 网格管线的无缝优化。** 2DGS 用平面高斯并提供网格提取；GOF 从 opacity level set 提取表面。两者都需要独立训练/优化、提取和纹理策略，不能仅凭渲染观感替代 MVS 的几何证据。
6. **“看不见的表面”只能作为先验生成的假设，不能作为已测量的重建事实。** 任何 prior-only completion 都必须采用 holdout protocol，并将“对保留可见表面的泛化”与“真正未观测表面的合理外推”分开报告。

## 2. 当前基线与可操作的 COLMAP 选择

### 2.1 当前实现和约束

当前 pipeline 使用：

- `patch_match_stereo --PatchMatchStereo.geom_consistency true`；
- `--PatchMatchStereo.gpu_index` 支持当前单卡/双卡配置；
- `stereo_fusion --input_type geometric`；
- 后续 Screened Poisson 和 COLMAP 照片贴图。

这意味着“几何一致性”不是只影响一个可视化选项，而是会影响深度图过滤、fusion 输入和最终网格支撑。仓库现有诊断已经区分了点云投影覆盖、网格支撑与无支撑外插；继续调参时应保留这些分层口径，而不是只看面数、点数或浏览器截图。

### 2.2 建议的选择/一致性矩阵

每一组都固定：Git 提交、COLMAP 版本、输入图片及 SHA-256、SfM sparse model、GPU、稠密分辨率、Poisson 参数和贴图器。只改变一项，并保存实际生成的 `patch-match.cfg`、阶段日志和 `job_manifest.json`。

| 对照 | 只改变的量 | 要回答的问题 | 主要代价/否决信号 |
| --- | --- | --- | --- |
| C0 | 当前自动源图选择（基线） | 当前默认是否稳定 | 作为所有后续组的参照，不把历史任务混入 |
| C1 | 自动源图数量下降 | 少源图能否降低时间/显存而不损失支撑 | 覆盖、注册后深度一致性或遮挡区域明显变差则否决 |
| C2 | 固定 source-view 列表 | 结果是否受自动选择抖动影响 | 只对有充分重叠的场景使用；固定列表不可跨场景照搬 |
| C3 | `geom_consistency=false`，仍启用过滤 | 速度收益是否值得几何一致性损失 | 仅可作为诊断/速度候选；出现漂浮点、错误深度或无支撑外插增加即否决 |
| C4 | 保持几何一致性，改变 fusion 的一致性阈值 | 过滤强弱对支撑与外插的边界 | 过滤放松填洞可能只是制造假面；必须报告支撑和无支撑外插两者 |

**推荐顺序：** 先 C1/C2，再做 C4，最后才做 C3。C1/C2 更接近源视图供给问题；C3 会改变几何约束，不能用“更快”单独通过。若 C2 需要编辑配置文件，配置必须作为任务输入的一部分进入 manifest，便于恢复和复现。

**不要这样解释结果：**

- 点数更多 ≠ 几何更准；
- 网格面积更大 ≠ 缺面被真实恢复；
- holdout 图像能渲染 ≠ 被遮挡表面被观测；
- 关闭一致性后速度更快 ≠ 该配置适合交付。

COLMAP 原始论文和官方入口： [Structure-from-Motion Revisited（CVPR 2016）](https://openaccess.thecvf.com/content_cvpr_2016/html/Schonberger_Structure-From-Motion_Revisited_CVPR_2016_paper.html)、[Pixelwise View Selection for Unstructured Multi-View Stereo（ECCV 2016）](https://www.microsoft.com/en-us/research/publication/pixelwise-view-selection-for-unstructured-multi-view-stereo/)、[COLMAP 官方项目](https://github.com/colmap/colmap)、[官方 FAQ](https://colmap.github.io/faq.html)。

## 3. 候选路线的取舍

### 3.1 PatchmatchNet：学习型 MVS 对照

- 原论文：[PatchmatchNet: Learned Multi-View Patchmatch Stereo（CVPR 2021）](https://openaccess.thecvf.com/content/CVPR2021/html/Wang_PatchmatchNet_Learned_Multi-View_Patchmatch_Stereo_CVPR_2021_paper.html)。
- 官方仓库：[FangjinhuaWang/PatchmatchNet](https://github.com/FangjinhuaWang/PatchmatchNet)。
- 论文贡献是将 coarse-to-fine PatchMatch 的传播和候选评分学习化；官方仓库的标准输入包含 `images/`、相机目录和 `pair.txt`，输出评估点云。

**可用场景：** 用同一 COLMAP 去畸变图像和相机，转换成 PatchmatchNet 输入，比较深度/点云密度、耗时、显存和 holdout 视图的重投影表现。`pair.txt` 的 source 数量必须记录，不能把它与 COLMAP 的 `__auto__` 选择当成同一个条件。

**关键 trade-off：**

- 优点：候选深度估计可作为学习型对照；可能减少手工 PatchMatch 搜索成本。
- 风险：模型权重、训练域、相机格式、尺度/坐标系和深度融合会引入新的失败面；官方输出是点云，不是本项目直接需要的带照片纹理 GLB。
- 决策门槛：只有在固定输入下，同时满足相机/尺度可对齐、点云转网格不引入明显无支撑外插、holdout 指标不劣于基线且资源账完整，才值得做独立 adapter；否则保留离线研究对照。

### 3.2 VGGT：前馈相机与几何先验

- 原论文：[VGGT: Visual Geometry Grounded Transformer（CVPR 2025）](https://arxiv.org/abs/2503.11651)。
- 官方仓库：[facebookresearch/vggt](https://github.com/facebookresearch/vggt)。

VGGT 的公开接口提供相机估计、深度图、point map、置信度和 3D tracks 等输出；官方仓库还提供导出 COLMAP sparse 结果的示例。这里的“导出可用”只表示接口存在，不表示它已经满足本系统网格质量要求。

**建议顺序：**

1. 只读输入图片，导出相机/深度/point map 和置信度；
2. 先与冻结的 COLMAP 相机解做尺度、旋转、平移对齐，再比较相机重投影和可见表面；
3. 若相机解可稳定对齐，再尝试把 VGGT 相机作为 COLMAP 初值或诊断信息，而不是直接替换 MVS；
4. 只有经过 holdout 和网格支撑验收后，才讨论接入。

**注意许可：** 官方仓库对原始 checkpoint 和商业使用有额外条款；在未完成许可审查前，不将权重打包进仓库、镜像或交付物，也不把网络下载作为在线任务依赖。

### 3.3 MASt3R：匹配与全局对齐先验

- 原论文：[Grounding Image Matching in 3D with MASt3R（ECCV 2024）](https://arxiv.org/abs/2406.09756)。
- 官方仓库：[naver/mast3r](https://github.com/naver/mast3r)。

MASt3R 的强项是从 pairwise 3D pointmap/局部特征得到匹配，并提供面向较大场景的全局对齐工作流。它不是“把一组图片直接变成已验收的贴图网格”的现成替换。

**建议定位：**

- 作为 COLMAP 注册失败或弱重叠图像的离线匹配诊断；
- 作为候选相机/匹配图，与 COLMAP 的几何验证对照；
- 不直接把 pairwise 匹配当作全局一致的相机模型或可测量的隐藏表面。

**工程约束：** 官方仓库代码采用 CC BY-NC-SA 4.0，checkpoint 还受训练数据条款约束；许可审查前不下载、不内置、不部署。若未来接入，应固定图像对生成、全局对齐、尺度恢复和异常对处理，并把每一步产物写入 manifest。

### 3.4 2DGS 与 GOF：高斯表示/表面提取路线

- 2DGS 原论文：[2D Gaussian Splatting for Geometrically Accurate Radiance Fields](https://arxiv.org/abs/2403.17888)；官方仓库：[hbb1/2d-gaussian-splatting](https://github.com/hbb1/2d-gaussian-splatting)。
- GOF 原论文：[Gaussian Opacity Fields: Efficient Adaptive Surface Reconstruction in Unbounded Scenes](https://arxiv.org/abs/2404.10772)；项目页：[Gaussian Opacity Fields](https://niujinshuchong.github.io/gaussian-opacity-fields)；官方仓库：[autonomousvision/gaussian-opacity-fields](https://github.com/autonomousvision/gaussian-opacity-fields)。

2DGS 用定向平面盘状高斯并加入深度/法线约束；其官方仓库记录了有界/无界场景的网格提取路径。GOF 以高斯定义 opacity field，再用等值面和 Marching Tetrahedra 提取网格，目标包含无界场景的自适应表面。

**与当前计划的差异：**

- 当前系统的验收对象是 MVS 支撑的网格和照片贴图 GLB；高斯路线的原生优势通常首先体现在视图合成/渲染表示；
- 训练/优化、checkpoint、显存、迭代停止、网格提取和纹理烘焙都需要另一个可追溯管线；
- “视觉上更完整”可能来自平滑或先验外推，不能等价为照片观测支持；
- 2DGS/GOF 的网格需要与当前 GLB 导出、照片贴图、面级支撑和版本回滚逐项对齐。

**决策：** 暂不改变默认交付路线。若另立原型，至少保存高斯场、提取参数、网格和纹理，并与 COLMAP 基线使用同一输入、相机和 holdout；没有这些证据时，只把它称为渲染/表面原型。

## 4. Prior-only 未观测区域：必须采用 holdout protocol

### 4.1 术语边界

- **Observed / measured：** 至少由输入图片中的可见证据和多视图几何支持的表面。
- **Holdout：** 输入中实际存在、但在推理时故意不提供给候选方法的图片或视角；它能检验跨视角泛化。
- **Unobserved completion：** 所有输入图片都没有观测到的表面。没有独立真值或后来采集的参考视角时，不能报告该表面的真实准确率。
- **Prior-only：** 候选方法可使用训练好的先验，但推理阶段不得读取 holdout 图片、其特征、深度、掩码或任何从 holdout 派生的几何。

这一区分尤其重要：单图/先验方法可以生成看起来合理的背面，但合理性不是测量。作为相关边界，Zero-1-to-3 的原论文明确是从单张 RGB 生成请求视角图像；这类生成视图可帮助重建，但遮挡面属于推断而非输入观测（[原论文 ICCV 2023](https://openaccess.thecvf.com/content/ICCV2023/html/Liu_Zero-1-to-3_Zero-shot_One_Image_to_3D_Object_ICCV_2023_paper.html)、[官方项目](https://zero123.cs.columbia.edu/)）。

### 4.2 推荐协议

对每个场景建立不可变的完整图片清单 `F`，逐张保存 SHA-256、相机/拍摄元数据（若有）和分组种子。每次候选评估执行以下步骤：

1. **按视角分组，而不是随机逐张漏图。** 例如按环带、立面或相机轨迹分成 `G1...Gk`；选择一个或多个整组作为 holdout，避免相邻图片泄漏。
2. **冻结输入。** 训练/推理输入为 `I = F \ H`，holdout 为 `H`。候选方法的 source pairs、相机初始化、阈值、模型选择和停止条件只能由 `I` 决定。
3. **记录可见性类别。** 对每个 holdout 像素/区域标为“由 `I` 中其他视角可见并有可能被测量”或“`I` 中真正未观测”。不能把后者混入普通重投影平均值。
4. **先做图像/相机级指标。** 报注册成功、相机重投影误差、holdout 视角渲染/投影覆盖和深度一致性；固定分辨率、遮罩、阈值和抽样，不用候选方法自己的置信度改变评测口径。
5. **再做几何级指标（有参考才做绝对误差）。** 若有激光/摄影测量/完整多面扫描参考，按 observed、可见 holdout、真正未观测区域分别报告 accuracy、completeness、F1 或距离分布。没有参考时，只报告投影覆盖、点支撑、无支撑外插和人工可视化，并明确它们不是绝对精度。
6. **防止先验泄漏。** 若候选 checkpoint 或训练集可能包含该场景、同一物体或相同纹理，记录来源并单列“可能有数据泄漏”的结果；未知时不得称为严格泛化。
7. **重复与失败保留。** 固定至少一个完全可复现的 split；资源允许时改变 holdout 组并保留所有成功、失败、超时和显存不足记录。不得挑最好 split。

建议的最小报告表：

| 分区 | 推理是否可见 | 可报告指标 | 不可作出的结论 |
| --- | --- | --- | --- |
| `I` 中可见表面 | 是 | 重投影、点/面支撑、参考几何误差（若有） | 不能据此证明背面完成 |
| `H` 中可见表面 | 否（评估时才读取） | holdout 覆盖、深度/几何误差、F1（有真值时） | 不能把生成得像照片称作真实测量 |
| 所有输入均不可见表面 | 否 | 仅在独立参考存在时报告几何误差；否则报告先验外推案例 | 不能宣称恢复真实形状或完整度 |

### 4.3 与当前项目的落地关系

当前批准计划不要求实现 prior-only completion。若未来立项，建议先在离线脚本中实现 split manifest 和评测，再决定是否接入 Worker。模型版本中应标记 `observed_reconstruction`、`holdout_evaluation` 或 `prior_completion`，避免在 Web 查看器和报告中把三类产物混称为同一种“完整模型”。

## 5. 推荐决策门槛与排期

### 5.1 第一优先级：不改变表示的 COLMAP 对照

- 固定当前 SfM，先做 C1/C2/C4；
- 报告阶段耗时、GPU/显存、注册数、重投影、点云/网格面数、点/面投影覆盖、支撑、无支撑外插和失败原因；
- 只要质量门槛不降且资源收益清楚，才考虑修改默认配置；
- 3000 px、GPU 数量及既有 benchmark 的数字仅作为仓库已有实测/估算记录，不在本页重新归因。

### 5.2 第二优先级：一个可复现的学习型离线对照

优先 VGGT（相机/point map 输出易于做诊断）或 PatchmatchNet（深度/点云对照），二选一，不同时引入三套权重。输入、许可、版本、权重哈希（若获授权后）和网络隔离方式都必须记录；没有授权权重时不运行、不下载。

### 5.3 暂不排期默认集成

MASt3R、2DGS、GOF 和 prior-only completion 保持研究候选。它们可以各自形成后续实验，但不能以“论文效果”“官方 demo”“渲染更好”作为当前系统验收证据。任何进入默认路径的候选都必须通过：

1. 固定输入和 holdout protocol；
2. 相机/尺度/坐标可追溯；
3. 网格与 GLB 交付兼容；
4. 质量不降、资源成本可解释；
5. 许可和离线部署可接受；
6. 失败可回滚到现有 COLMAP 版本。

## 6. 参考链接（已核对的原始/官方入口）

- [COLMAP 官方项目](https://github.com/colmap/colmap)；[COLMAP FAQ](https://colmap.github.io/faq.html)。
- [Structure-from-Motion Revisited](https://openaccess.thecvf.com/content_cvpr_2016/html/Schonberger_Structure-From-Motion_Revisited_CVPR_2016_paper.html)；[Pixelwise View Selection for Unstructured Multi-View Stereo](https://www.microsoft.com/en-us/research/publication/pixelwise-view-selection-for-unstructured-multi-view-stereo/)。
- [PatchmatchNet 原论文](https://openaccess.thecvf.com/content/CVPR2021/html/Wang_PatchmatchNet_Learned_Multi-View_Patchmatch_Stereo_CVPR_2021_paper.html)；[官方代码](https://github.com/FangjinhuaWang/PatchmatchNet)。
- [VGGT 原论文](https://arxiv.org/abs/2503.11651)；[官方代码](https://github.com/facebookresearch/vggt)。
- [MASt3R 原论文](https://arxiv.org/abs/2406.09756)；[官方代码](https://github.com/naver/mast3r)。
- [2DGS 原论文](https://arxiv.org/abs/2403.17888)；[官方代码](https://github.com/hbb1/2d-gaussian-splatting)。
- [GOF 原论文](https://arxiv.org/abs/2404.10772)；[官方项目页](https://niujinshuchong.github.io/gaussian-opacity-fields)；[官方代码](https://github.com/autonomousvision/gaussian-opacity-fields)。
- [Zero-1-to-3 原论文](https://openaccess.thecvf.com/content/ICCV2023/html/Liu_Zero-1-to-3_Zero-shot_One_Image_to_3D_Object_ICCV_2023_paper.html)；[官方项目](https://zero123.cs.columbia.edu/)。

