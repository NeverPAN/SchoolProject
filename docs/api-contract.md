# API 契约（v1）

- 前缀 `/api/v1`；认证方式 `Authorization: Bearer <JWT>`；
- 业务错误统一结构 `{"code": "<稳定错误码>", "message": "<可读信息>"}`；
- 校验错误 422（FastAPI 标准）。

## 认证

| 方法 | 路径 | 说明 | 成功 |
| --- | --- | --- | --- |
| POST | `/auth/register` | body: `{username(3-64), password(6-128)}` | 201 `UserOut`；409 用户名已存在 |
| POST | `/auth/login` | body: `{username, password}` | 200 `{access_token, token_type, user}`；401 |
| POST | `/auth/logout` | 需登录（JWT 无状态，前端丢弃） | 200 `UserOut` |
| GET | `/me` | 当前用户 | 200 `UserOut`；401 |

## 项目

| 方法 | 路径 | 说明 | 成功 |
| --- | --- | --- | --- |
| GET | `/projects` | 本人项目列表（含统计） | 200 `[ProjectOut]` |
| POST | `/projects` | body: `{name(1-128), description?}` | 201 |
| GET | `/projects/{id}` | 详情（含 images 摘要与质量汇总） | 200；404 非本人/不存在 |
| PATCH | `/projects/{id}` | `{name?, description?}` | 200 |
| DELETE | `/projects/{id}` | 运行中任务时 409；级联删除文件 | 200 |

## 图片

| 方法 | 路径 | 说明 | 成功 |
| --- | --- | --- | --- |
| POST | `/projects/{id}/images` | multipart `files[]`（≤200/批） | 201 `[ImageOut]`；部分失败 201+`partial_errors`；类型/损坏 400；重复 409；超限 413 |
| GET | `/projects/{id}/images` | 图片列表 | 200 |
| GET | `/images/{id}/thumbnail` | JPEG 缩略图（流） | 200 image/jpeg |
| GET | `/images/{id}/file` | 原图（流） | 200 |
| DELETE | `/images/{id}` | 删除记录与文件 | 200 |
| POST | `/projects/{id}/quality-check` | 对 pending 图片执行质量检查 | 200 `{checked, ok, warning, invalid}` |

`ImageOut`: `{id, project_id, original_filename, stored_name, file_size, width, height, format, checksum, exif, quality_status(pending/ok/warning/invalid), quality_notes, has_thumbnail, created_at}`

## 重建任务

| 方法 | 路径 | 说明 | 成功 |
| --- | --- | --- | --- |
| POST | `/projects/{id}/jobs` | body: `{preset(fast/standard/high), matcher(auto/exhaustive/sequential), enable_dense, enable_mesh}` | **202** `JobOut`；400 图片<3 或 COLMAP 不可用；409 已有进行中任务 |
| GET | `/projects/{id}/jobs` | 项目任务列表 | 200 |
| GET | `/jobs/{id}` | 详情（含 events） | 200 |
| GET | `/jobs/{id}/events` | 阶段事件 | 200 `[JobStageEventOut]` |
| GET | `/jobs/{id}/logs?stage=&tail=` | 纯文本日志 | 200 text/plain |
| POST | `/jobs/{id}/cancel` | RUNNING→CANCEL_REQUESTED；QUEUED→CANCELLED | 200；409 状态不允许 |
| POST | `/jobs/{id}/resume` | 仅 FAILED/CANCELLED；输入变化 409 INPUT_CHANGED | 200 |

`JobOut`: `{id, project_id, status, current_stage, progress(0-100,估算), pipeline_mode(colmap/mock), engine_version, config{...}, started_at, finished_at, error_stage, error_code, error_message, created_at}`

状态机：`CREATED→VALIDATING→QUEUED→RUNNING→(SUCCEEDED|FAILED|CANCEL_REQUESTED→CANCELLED)`；`FAILED/CANCELLED→(resume)→RESUMING→QUEUED`
阶段：`IMAGE_VALIDATION, FEATURE_EXTRACTION, FEATURE_MATCHING, SPARSE_RECONSTRUCTION, IMAGE_UNDISTORTION, DENSE_RECONSTRUCTION, STEREO_FUSION, MESHING, EXPORTING`
事件状态：`running / completed / skipped(检查点) / failed / cancelled`

## 模型

| 方法 | 路径 | 说明 | 成功 |
| --- | --- | --- | --- |
| GET | `/projects/{id}/models` | 项目模型列表 | 200 |
| GET | `/models/{id}` | 详情（含 versions） | 200 |
| GET | `/models/{id}/versions` | 版本列表（含模型快照） | 200 |
| GET | `/models/{id}/download` | 下载（attachment） | 200 |
| GET | `/models/{id}/file` | 查看器数据（前端 Blob 加载） | 200 |
| GET | `/models/{id}/preview` | PNG 预览图 | 200；404 |
| GET | `/models/{id}/manifest` | 任务级 manifest JSON | 200 |
| DELETE | `/models/{id}` | 删除记录（保留同任务其他产物） | 200 |

`ModelOut`: `{id, project_id, job_id, model_type(sparse_pointcloud/dense_pointcloud/mesh/web), format(ply/glb), file_size, vertex_count, face_count, point_count, stage_origin, created_at}`

## 健康

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务/COLMAP 可用性/GPU |
| GET | `/health/db` | 数据库连通 |
| GET | `/health/worker` | Redis/队列状态 |
