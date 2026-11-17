# 测试数据集说明

## sample-data/smoke-30（合成数据集）

- 生成脚本：`scripts/generate_synthetic_dataset.py`（纯 numpy/PIL 软件光栅化，无外部依赖）
- 内容：室内场景（棋盘地面 + 两面纹理墙 + 两个纹理立方体），30 个环绕视角，960×960 JPEG
- 相机：SIMPLE_PINHOLE，f=700px（960px 图），真值保存在 `ground_truth.json`
- 用途：**Mock 模式演示与前端联调**；离线可复现
- 已知限制：对真实 COLMAP 的 SfM 匹配率不足（特征跨视角匹配率低），不用于真实重建测试

重新生成：
```bash
python scripts/generate_synthetic_dataset.py --out sample-data/smoke-30 --num 30 --size 960
```

## south-building（真实数据集，不入 Git）

- 来源：COLMAP 官方示例数据（UNC Chapel Hill South Building）
  https://demuc.de/colmap/datasets/ → https://github.com/colmap/colmap/releases/download/3.11.1/south-building.zip
- 规模：128 张 3072×2304 JPEG（约 420MB），视频式环绕采集
- 用途：真实 SfM+MVS 全管线端到端测试；128 张全量重建已验证（126 张注册）
- 采集顺序即文件名顺序（P1180141.JPG…），适合 sequential matcher

## 数据标准对照（指南 §1.4）

| 项 | Smoke（合成） | Pilot/Main（south-building） |
| --- | --- | --- |
| 数量 | 30 | 128 |
| 分辨率 | 960×960 | 3072×2304（≈7MP） |
| 总量 | 5.6MB | 420MB |
| 视角重叠 | 高（12° 间隔） | 高（视频帧） |
