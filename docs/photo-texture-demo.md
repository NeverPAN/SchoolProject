# 演示项目 #7：照片贴图版

模型 #6（v3）由已有 `mesh.ply` 和 40 张去畸变照片生成。原点云、网格、旧 GLB 及在线重建引擎均未替换。

## 生成与验证

- 独立环境运行 COLMAP 4.2.1 CPU 版 `mesh_texturer`，参数 `--MeshTextureMapping.num_threads 8 --MeshTextureMapping.texture_scale_factor 0.5 --MeshTextureMapping.atlas_patch_padding 4`。本次耗时 6.84 秒（仅贴图阶段，不含 GLB 导出），图集为 8192 × 3016。
- 98,129 个面中，86,380 个取得照片视角（88.03% 按面数，非表面积覆盖率）。未分配视角的面用中性材质。贴图不修改几何，也不补洞。
- 同一工作区运行 `model_orientation_aligner --method MANHATTAN-WORLD`，得到建筑竖直轴。用相机图像上方向判定正负，导出时将上方向 `[-0.0527163, -0.975772, 0.212345]` 映射到 +Y，原网格包围盒中心平移至原点。查看器不重复应用任务级朝向。
- `backend/app/reconstruction/photo_export.py` 保留面角 UV、嵌入 PNG、为未观察区域保留独立材质。导出后重新读入，核对两组面的坐标和绕序均与旋转后的源网格一致。GLB 20,490,304 字节，在线下载与本地产物 SHA-256 一致：`aee23ecd5864d0b262ffe33020b5f5cd11d2172c511b5533018180f133962c2c`。
- 后端 52 项回归测试通过，前端生产构建通过。未进行浏览器同视角截图对照，不能据此给出画面质量评分。