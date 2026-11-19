# 开发总结 7：质量提升方案调研（文献综述与选型）

> 目标：在"质量不降、力争更好"约束下，为阶段 6 的优化选择有文献依据、可在本项目落地的方案。
> 结论先行：本轮落地 **双 GPU 并行**（纯速度）、**稠密 3000px 对照**（主质量抓手）、**Screened Poisson point_weight 调参**；学习型前馈重建与高斯泼溅明确为"评估后暂缓"，理由与文献如下。

## 1. 候选方案族

### A. 经典管线参数优化（COLMAP 自身）

COLMAP 的 MVS（[Schönberger & Frahm CVPR 2016; Schönberger et al. ECCV 2016 的 pixelwise view selection](https://colmap.github.io/)）质量主要由稠密分辨率与深度图过滤参数决定。官方 [FAQ](https://colmap.github.io/faq.html) 与社区参数笔记 [colmap-parameters](https://github.com/mwtarnowski/colmap-parameters)：

- 弱纹理表面：增大 `window_radius`（默认 5）；更稠密采样：`window_step` 2→1；
- 点云过稀/空洞：放松 `filter_min_ncc`（默认 0.1）；更干净表面：提高之；
- 深度密度还受 `stereo_fusion` 的 `max_reserve_dist`、`min_num_pixels` 影响。
- **评估**：参数窗口小、需逐场景验证；我们 2026-11 数据（木质桥梁/砖墙）纹理强，默认过滤已是较优配置。→ 本轮只做低风险的 `point_weight`（网格阶段）实验，不动深度过滤默认值。

### B. 稠密分辨率（本轮主抓手）

像素级 MVS 的深度精度与细节密度随分辨率近乎单调提升；我们已实测 1000px→2000px 的巨大收益（空洞根因）。2000→3000px 逻辑相同，代价是耗时 ×~2.25（面积比）——由双卡并行抵消。→ **实验落地**。

### C. 网格化替代：Delaunay 图割 vs Screened Poisson

- Screened Poisson（[Kazhdan & Hoppe, ACM TOG 2013](https://www.cs.jhu.edu/~misha/Code/PoissonRecon/)）：Watertight、平滑，但弱支撑区会外插平滑面（我们已用 z-aware 指标量化过这一现象）。
- Delaunay 图割（[Labatut et al. 2007](https://hal.archives-ouvertes.fr/inria-00263939/)；[Jancosek & Pajdla CMPMVS, CVPR 2011](https://ieeexplore.ieee.org/document/5995466)）：保留弱支撑表面、边界更贴点云，适合含天空/水面的室外场景；COLMAP 提供 `delaunay_mesher`，但其输入是**稀疏模型 + 可见性轨迹**，直接 mesh 稠密融合云需要把融合点导入模型并重建可见性（COLMAP 无现成命令），工程成本不小。
- **评估**：本轮做**可行性试探**（能跑则对比，跑不通如实记录），不作为交付默认。

### D. 学习型前馈重建（DUSt3R / MASt3R / VGGT）

- 综述与评测：[Wu et al. 2025, Geo-spatial Information Science](https://www.tandfonline.com/doi/full/10.1080/10095020.2025.2597491) 报告 VGGT 在完整性上对 COLMAP 有显著优势（部分场景 ~50%）；[Zhang et al. 2025 综述](https://arxiv.org/pdf/2507.08448) 梳理 DUSt3R→MASt3R→VGGT 谱系；[Learning-based MVS survey](https://arxiv.org/html/2408.15235v3)、[MVS 算法综述 2025](https://dl.acm.org/doi/10.1016/dsp.2025.105291)、[Awesome-MVS](https://github.com/walsvid/Awesome-MVS)。
- 经典深度回归 MVS：[MVSNet ECCV 2018]、[PatchmatchNet ICCV 2021]、[MVSFormer TMLR 2023]。
- **评估**：优点是快、完整性好；**风险**：输出为带尺度的点/深度图，相机精度与一致性弱于 COLMAP（评测原话：COLMAP "wins on geometric consistency for mesh-grade outputs"），网格级质量未必更好，且引入 torch 权重依赖与新的失败面。与"质量至少不降"约束冲突风险高。→ **本轮不集成**，列为后续课题（可先做 VGGT 离线对照实验再决定）。

### E. 高斯泼溅表面重建（3DGS / SuGaR / 2DGS / GOF）

- [3DGS, Kerbl et al. SIGGRAPH 2023]；[SuGaR, CVPR 2024]（表面对齐正则，可提取网格）；[2DGS, Huang et al. SIGGRAPH 2024](https://github.com/hbb1/2d-gaussian-splatting)（2D 盘状高斯，几何更准）；[GOF, Yu et al. SIGGRAPH Asia 2024](https://github.com/autonomousvision/gaussian-opacity-fields)（不透明场 level-set 提取网格，无界场景强）；2025 年已有 [表面重建综述](https://arxiv.org/html/2408.15235v3) 与 Sparse2DGS/ULSR-GS 等分支。
- **评估**：渲染质量极高，但网格是从高斯场提取的近似面，且训练/提取管线与本系统的"网格+贴图 GLB 交付"形态不同；对"网格质量"未必优于 2000px MVS，且引入重量级训练管线。→ **评估后暂缓**（此前第二阶段调研结论一致）。

### F. 相关工程组件

- 多卡：COLMAP `patch_match_stereo` 原生支持按图片分配多 GPU（`gpu_index "0,1"`）——纯吞吐优化，不影响单图深度质量，风险最低。
- 贴图：COLMAP texturer 沿用 [Waechter et al., "Let There Be Color!", 2014] 的视角选择思想，本轮不换。

## 2. 本轮决策

| 方案 | 决定 | 依据 |
| --- | --- | --- |
| 双 GPU（`COLMAP_GPU_INDEX`） | ✅ 实施 | 纯速度、零质量风险、代码量小 |
| 稠密 3000px 对照 | ✅ 实验 | 分辨率是已验证的质量主参数；双卡抵消耗时 |
| Poisson `point_weight` 对照 | ✅ 实验 | 低成本；screened Poisson 权衡"数据项 vs 平滑项"的正规手段 |
| Delaunay 试探 | ✅ 可行性试探 | 有文献支撑但集成成本高；跑不通则如实记录 |
| 学习型前馈 MVS / GS 表面重建 | ⏸ 暂缓 | 集成成本与"质量不降"约束冲突；文献已归档 |

## 3. 参考文献

1. Schönberger & Frahm. Structure-from-Motion Revisited. CVPR 2016；Schönberger et al. Pixelwise View Selection for Unstructured Multi-View Stereo. ECCV 2016.
2. Kazhdan & Hoppe. Screened Poisson Surface Reconstruction. ACM TOG 32(3), 2013.
3. Labatut, Ponce, Bouget. Robust and Accurate Surface Reconstruction using Delaunay Triangulation. 2007；Jancosek & Pajdla. Multi-view Reconstruction Preserving Weakly-Supported Surfaces. CVPR 2011.
4. Waechter et al. Let There Be Color! Large-Scale Texturing of 3D Reconstructions. EG 2014.
5. Yao et al. MVSNet. ECCV 2018；Wang et al. PatchmatchNet. ICCV 2021；Tian et al. MVSFormer. TMLR 2023.
6. Kerbl et al. 3D Gaussian Splatting. SIGGRAPH 2023；Guédon & Lepetit. SuGaR. CVPR 2024；Huang et al. 2DGS. SIGGRAPH 2024；Yu et al. Gaussian Opacity Fields. SIGGRAPH Asia 2024.
7. Wang et al. VGGT. 2025；Leroy et al. DUSt3R/MASt3R. 2024. 评测：[DUSt3R/MASt3R/VGGT evaluation, 2025](https://www.tandfonline.com/doi/full/10.1080/10095020.2025.2597491)；综述：[Feed-forward 3D Reconstruction review, 2025](https://arxiv.org/pdf/2507.08448)。
8. [COLMAP FAQ](https://colmap.github.io/faq.html)；[colmap-parameters](https://github.com/mwtarnowski/colmap-parameters)；[Learning-based MVS survey](https://arxiv.org/html/2408.15235v3)；[MVS 算法综述 2025](https://dl.acm.org/doi/10.1016/dsp.2025.105291)；[Awesome-MVS](https://github.com/walsvid/Awesome-MVS)。
