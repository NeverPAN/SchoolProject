"""阶段 8 原型：查看器框选区域的提取任务。

诚实边界：本模块做的是**区域裁剪导出**（把用户框选的网格部分导出为独立模型并
生成报告），**不是局部重建**——不会为新照片重算该区域的几何。真正的局部重建
可行性研究见 docs/dev-summary/10-local-reconstruction-feasibility.md。
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def run(job_id: int, config: dict) -> tuple[list[dict], dict]:
    """执行区域提取：裁剪目标模型到选区包围盒，登记为 stage_origin=LOCAL_REGION。"""
    import trimesh
    from trimesh.exchange.gltf import export_glb

    from app.db.session import SessionLocal
    from app.models.entities import Model, ModelRegion, ReconstructionJob
    from app.services.image_service import project_output_dir

    with SessionLocal() as db:
        job = db.get(ReconstructionJob, job_id)
        region = db.get(ModelRegion, int(config["region_id"]))
        if region is None:
            raise ValueError(f"选区不存在: {config.get('region_id')}")
        model = db.get(Model, region.model_id)
        if model is None or not Path(model.file_path).is_file():
            raise ValueError("目标模型或其文件不存在")
        bbox_min = (region.bbox_min_x, region.bbox_min_y, region.bbox_min_z)
        bbox_max = (region.bbox_max_x, region.bbox_max_y, region.bbox_max_z)
        project = job.project

    mesh = trimesh.load(model.file_path, force="mesh", process=False)
    centers = mesh.triangles.mean(axis=1)
    mask = ((centers >= bbox_min) & (centers <= bbox_max)).all(axis=1)
    if not mask.any():
        raise ValueError("选区内没有任何面片，请检查包围盒")
    region_mesh = trimesh.Trimesh(
        vertices=mesh.vertices, faces=mesh.faces[mask], process=False,
        visual=mesh.visual,
    )
    region_mesh.remove_unreferenced_vertices()

    out_dir = Path(project_output_dir(project, job_id))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "region.glb"
    path.write_bytes(export_glb(trimesh.Scene({"region": region_mesh})))

    report = {
        "note": "阶段8原型：区域提取（非局部重建）；真正的局部重建可行性研究见 docs/dev-summary/10",
        "region_id": region.id,
        "target_model_id": model.id,
        "target_model_file": Path(model.file_path).name,
        "bbox_min": list(bbox_min), "bbox_max": list(bbox_max),
        "source_faces": int(len(mesh.faces)), "source_vertices": int(len(mesh.vertices)),
        "region_faces": int(mask.sum()), "region_vertices": int(len(region_mesh.vertices)),
        "region_face_ratio": round(float(mask.mean()), 6),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (out_dir / "region_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    records = [{
        "model_type": "mesh", "format": "glb", "file_path": str(path),
        "file_size": path.stat().st_size, "stage_origin": "LOCAL_REGION",
        "vertex_count": int(len(region_mesh.vertices)), "face_count": int(mask.sum()),
        "point_count": 0,
    }]
    manifest = {
        "job_id": job_id, "pipeline_mode": "local_update",
        "note": report["note"], "region_id": region.id,
        "target_model_id": model.id, "resolved_params": {"bbox_min": list(bbox_min), "bbox_max": list(bbox_max)},
        "outputs": [r["file_path"] for r in records],
    }
    return records, manifest
