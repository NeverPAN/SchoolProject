# 数据库设计

> SQLAlchemy 2.x 声明式模型（`backend/app/models/entities.py`），Alembic 迁移管理。
> 第一阶段字段按验收需要精简，保证 图片 → 任务 → 模型/版本 可追溯。

## E-R 概览

```text
User 1──N Project 1──N Image
              │ 1──N ReconstructionJob 1──N JobStageEvent
              │ 1──N Model 1──N ModelVersion
              └──────────（Image.checksum 进入任务 input_manifest）
```

## 表结构

### users
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | |
| username | VARCHAR(64) UNIQUE | 登录名 |
| password_hash | VARCHAR(255) | bcrypt，不存明文 |
| role | VARCHAR(16) | user / admin |
| created_at | DATETIME | UTC |

### projects
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | |
| owner_id | FK users.id | 数据归属 |
| name / description / status | | status=active |
| created_at / updated_at | | |

### images
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | |
| project_id | FK projects.id | |
| original_filename | VARCHAR(255) | 展示用（已清洗） |
| stored_path / thumbnail_path | VARCHAR(1024) | UUID 存储名，路径由服务端生成 |
| file_size | BIGINT | 字节 |
| width / height / format | | PIL 实测 |
| checksum | SHA-256 | 项目内 UNIQUE（去重） |
| exif_json | TEXT | Make/Model/DateTimeOriginal 等 |
| quality_status / quality_notes | | pending/ok/warning/invalid |

### reconstruction_jobs
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | |
| project_id | FK | |
| status | | 状态机（见 api-contract.md） |
| current_stage / progress | | progress 为阶段权重估算 |
| config_json | TEXT | preset/matcher/enable_dense/enable_mesh/use_gpu/mode |
| pipeline_mode / engine_version | | colmap / mock；COLMAP 版本串 |
| input_manifest | TEXT | 图片清单（id/filename/checksum/分辨率）— 恢复校验依据 |
| started_at / finished_at / error_stage / error_code / error_message | | |
| cancel_requested | BOOLEAN | Worker 在阶段边界与子进程轮询中响应 |

### job_stage_events
`job_id, stage, status(running/completed/skipped/failed/cancelled), started_at, finished_at, duration_ms, log_path, message`

### models
`project_id, job_id, model_type(sparse_pointcloud/dense_pointcloud/mesh/web), format(ply/glb), file_path, file_size, vertex_count, face_count, point_count, stage_origin, created_at`

### model_versions
`project_id, model_id, version_number(递增), parent_version_id, change_type(initial/append_images/manual), change_description, manifest_path, created_at`

## 迁移记录

| 版本 | 内容 |
| --- | --- |
| 19788f1a2e2f | init schema（7 张表） |
| 22651a4c6e93 | add projects.updated_at |
