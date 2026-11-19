# 开发总结 9：阶段 7 补全 —— 追加照片 → 版本更新（E6）

> 指南阶段 7 的缺口：追加照片、新模型版本、更新前后对比；验收点"追加照片后旧版本仍保留、更新失败不破坏原模型"。
> 版本记录表（`model_versions`）第二阶段已有，缺的是**追加照片语义的版本链**。

## 1. 实现

- 追加照片：直接向已有项目继续上传（现有上传接口即可），无需新建项目。
- 发布时（`workers/jobs.py::_publish`）：
  - 查询项目内**此前最近一次**的 web 模型版本（`latest_web_version_before_job`）；
  - 统计自上次任务开始后新上传的图片数（`count_images_added_since`）；
  - 有追加 → 新 web 模型的版本号**续接父版本**（v2、v3…），`change_type=append_images`，`parent_version_id` 指向上一个版本，描述"追加 N 张照片后更新"；
  - 无追加的重跑 → `change_type=rerun`；
  - 旧模型与其版本**保持不动**（同事务原子发布）。
- 任务清单 `resolved_params` 记录实际生效参数（depth/GPU 卡数等），模型可追溯。

## 2. 测试（pytest，2 项新测试，共 78 项通过）

- `test_append_photos_continues_version_chain`：4 张 → 任务 v1；追加 3 张 → 新任务 → 新 web 模型 `version_number=2`、`change_type=append_images`、`parent_version_id` 指向 v1、描述含"追加 3 张"；**旧模型下载字节与版本完全不变**；新模型可下载。
- `test_rerun_without_new_photos_marks_rerun`：同输入重跑 → `change_type=rerun`。

## 3. E6 真实数据实验（应用内，双卡 3000px）

- 项目 1（South Building）：原 40 张 → 任务 #7 模型 v1（#34，51.1MB）；
- **追加 10 张**（P1180181–P1180190，按字节哈希确认此前未用过）→ 任务 #12（50 张，3000px 双卡）；
- 记录旧模型 SHA-256（`/tmp/phase2-run.EnRwa6/old-model34.sha`）用于事后比对。

结果（任务 #12 完成后填写）：

- [ ] 新 web 模型版本号/类型/父版本
- [ ] 旧模型 #34 SHA-256 不变
- [ ] 前后对比：点数/面数/覆盖
